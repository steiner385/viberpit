# Plotroom Pipeline Replica — Phase 3 (Data Gap-Fill) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the honest-null Phase-1 fields with real data: `mergeEtaSim` (multi-train merge ETA), `timeline` (PrTimeline → live Waterfall), `repos[].deploy` (health-polled environments + backlog + supersession chain), and `touchesWorkflows`/`workflowImpact` — no frontend changes needed (the ported components already render all of it).

**Architecture:** Four backend subsystems ported from plotroom's server (TS→Python), re-specified in `.superpowers/sdd/phase3-plotroom-spec.md` (functional spec extracted from plotroom@5166b52). New ccswitch-owned tables live in the main app DB (SQLAlchemy models, nullable columns — dev `init_db` backfills); the plotroom-migrated `pipeline_history.db` stays read-only. Deploy ancestry uses the GitHub compare API (no local clones). Data flows into the existing `dashboard_state` assembler and out over the existing SSE feed.

**Tech Stack:** FastAPI + SQLAlchemy + httpx + PyYAML (all existing deps), pytest. Reference algorithms: plotroom@5166b52 `server/estimator/queue.ts`, `server/estimator/deploy-status.ts`, `server/workflow-impact.ts`, `server/poller.ts` timeline capture.

**Spec:** `docs/superpowers/specs/2026-07-12-plotroom-pipeline-replica-design.md` (§4, §8 phase 3)

## Global Constraints

- **Branch:** `feat/plotroom-pipeline-phase3` off `origin/master`; verify `git branch --show-current` before EVERY commit. Commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **DB rule (CLAUDE.md):** new model columns/tables NULLABLE — `create_all` never ALTERs; `init_db` backfills missing nullable columns. New tables are ccswitch-owned in the main app DB; `server/pipeline/history.py` stays read-only.
- **Server gates per task:** `ruff check server/ src/`; `mypy server/ --ignore-missing-imports`; targeted pytest via `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <paths> -q`. Full-suite (Task 9) adds `--ignore=server/tests/daemon_properties --ignore=server/tests/test_daemon_properties.py --deselect server/tests/test_eta_parity_live.py -k "not copilot_proxy_resolve and not test_skill_crud_roundtrip"`.
- **Web gates (Task 9; no web source changes expected):** in `web/`: `npx vitest run && npx tsc --noEmit && npx eslint src && npm run build`.
- **API tests with a separate seeding connection need a temp-file `CCSWITCH_DB_URL`** (sqlite `:memory:` seed-visibility flake — memory note).
- **New orchestrator methods called by API routes** need RemoteOrchestrator proxy + RPC registration + facade one-liner; the facade reflection guard (`test_orchestrator_facade.py`) enforces the facade half.
- **Honest nulls:** every value not yet observable stays null — never fabricate (plotroom Waterfall/PrRow render gaps honestly).
- **Wire-shape truth:** `web/src/pipeline/types.ts` (`MergeEtaSimulation`, `PrTimeline`, `DashboardState.repos[].deploy`, `PrView.touchesWorkflows/workflowImpact`) — camelCase keys.

---

### Task 0 (fold into Task 1 Step 1): persist the plotroom functional spec

The two exploration reports this plan was written from must be available to implementers. Write `.superpowers/sdd/phase3-plotroom-spec.md` containing the "COMPLETE FUNCTIONAL SPECIFICATION: PLOTROOM SERVER SUBSYSTEMS" report (mergeEtaSim / PrTimeline / deploy / workflow-impact algorithms with plotroom file:line cites) — the controller has it in-session; implementers receive the file path. Every task below cites section numbers from that file as `SPEC §N`.

### Task 1: DB models — merged_prs timeline, group_runs, group_failures, repo_deploy_config

**Files:**
- Modify: `server/db/models.py` (append four models, following `MergeQueueObservation` at models.py:230-254 as the idiom)
- Test: `server/tests/test_phase3_models.py`

**Interfaces:**
- Produces (consumed by Tasks 2-6): SQLAlchemy models
  - `MergedPr`: `repo: str (pk part)`, `number: int (pk part)`, `title: str|None`, `url: str|None`, `merged_at: str` (ISO), `merge_commit_sha: str|None`, `created_at: str|None`, `first_green_at: str|None`, `enqueued_at: str|None`, `qa_live_at: str|None`, `prod_live_at: str|None` (composite PK (repo, number); all timestamp columns TEXT ISO-8601, nullable except merged_at)
  - `GroupRun`: `id (pk)`, `repo (indexed)`, `duration_secs: int`, `completed_at: str` (ISO, unique together with repo)
  - `GroupFailure`: `id (pk)`, `repo (indexed)`, `check_name: str`, `group_sha: str`, `observed_at: str`, `conclusion: str|None`; unique (repo, group_sha, check_name)
  - `RepoDeployConfig`: `repo: str (pk)`, `environments: str` (JSON text: `[{"name","healthUrl","auto","shaKey"}]`), `enabled: bool default True`

- [ ] **Step 1: Branch + persist SPEC file** — `git fetch origin && git checkout -b feat/plotroom-pipeline-phase3 origin/master`; controller has already written `.superpowers/sdd/phase3-plotroom-spec.md` (verify it exists; if missing, STOP and report NEEDS_CONTEXT).
- [ ] **Step 2: Failing test** — create/query each model through a temp-file SQLite session (copy the DB-fixture idiom from an existing models/api test; NOT `:memory:`):

```python
# server/tests/test_phase3_models.py
"""Phase-3 tables: timeline, group runs/failures, deploy config (SPEC §1-§3)."""
import pytest
from server.db.models import GroupFailure, GroupRun, MergedPr, RepoDeployConfig


@pytest.mark.asyncio
async def test_merged_pr_upsert_semantics(db_session):
    row = MergedPr(repo="o/r", number=7, merged_at="2026-07-12T00:00:00Z",
                   title="t", url="u", merge_commit_sha="abc")
    db_session.add(row)
    await db_session.commit()
    got = await db_session.get(MergedPr, ("o/r", 7))
    assert got.first_green_at is None and got.qa_live_at is None


@pytest.mark.asyncio
async def test_group_run_and_failure_rows(db_session):
    db_session.add(GroupRun(repo="o/r", duration_secs=600,
                            completed_at="2026-07-12T00:00:00Z"))
    db_session.add(GroupFailure(repo="o/r", check_name="e2e", group_sha="g1",
                                observed_at="2026-07-12T00:00:00Z", conclusion="FAILURE"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_repo_deploy_config_roundtrip(db_session):
    import json
    envs = [{"name": "qa", "healthUrl": "https://qa/health", "auto": True, "shaKey": "commitSha"}]
    db_session.add(RepoDeployConfig(repo="o/r", environments=json.dumps(envs)))
    await db_session.commit()
    got = await db_session.get(RepoDeployConfig, "o/r")
    assert json.loads(got.environments)[0]["name"] == "qa"
```
(Adapt the `db_session` fixture name/shape to the repo's existing test fixtures — find with `grep -rln "async_session\|db_session" server/tests | head -3` and reuse.)
- [ ] **Step 3: Implement the four models** (mirror `MergeQueueObservation`'s style: `__tablename__`, typed columns, indexes; all nullable except noted).
- [ ] **Step 4: RED→GREEN, ruff+mypy, commit** — `feat(pipeline): phase-3 tables — merged_prs timeline, group runs/failures, deploy config`.

---

### Task 2: Group-run / group-failure recorder

**Files:**
- Create: `server/pipeline/group_history.py`
- Modify: `server/github/pr_population.py` (the merge-group enrichment already parses group checks — hook there; read `_enrich_with_eta` / `parse_merge_group_response` usage around pr_population.py:205-288 first)
- Test: `server/tests/test_group_history.py`

**Interfaces:**
- Consumes: parsed merge-group checks (name, status, conclusion, startedAt, completedAt) per repo/batch oid, which the population cycle already has in hand.
- Produces (consumed by Task 3): async store functions
  - `record_group_outcome(repo: str, group_sha: str, checks: list[dict], now_iso: str) -> None` — SPEC §1: if ALL checks concluded SUCCESS|SKIPPED|NEUTRAL → insert `GroupRun(duration = max(completedAt) - min(startedAt))` (dedupe on (repo, completed_at)); for each FAILURE|TIMED_OUT|STARTUP_FAILURE check → insert `GroupFailure` (dedupe on (repo, group_sha, check_name)).
  - `group_run_samples(repo: str, limit: int = 20) -> list[int]` (newest first), `count_group_runs(repo, since_iso) -> int`, `count_group_ejects(repo, since_iso) -> int` (COUNT(DISTINCT group_sha)).

- [ ] **Step 1: Failing tests** — pure store tests: record a fully-green group (assert one GroupRun with correct duration; re-record same completed_at → still one), a failing group (assert GroupFailure rows, deduped), then the three read functions (samples order, counts windowed).
- [ ] **Step 2: Implement the store**, then wire the recorder into the population cycle at the point where merge-group check details are parsed (guard: only record when every check has a terminal status; skip quietly when timestamps missing — honest gaps). Recording must never break the poll cycle: wrap in try/except with a logged warning.
- [ ] **Step 3: RED→GREEN + an integration-style test** proving a population cycle with a green merge group produces a GroupRun row (reuse the poller test fixtures — find with `grep -rln "pr_population" server/tests | head`).
- [ ] **Step 4: Gates + commit** — `feat(pipeline): record merge-group run durations + ejects (mergeEtaSim substrate)`.

---

### Task 3: mergeEtaSim — the multi-train simulator

**Files:**
- Create: `server/pipeline/merge_sim.py`
- Modify: `server/pipeline/dashboard_state.py` (`queue_view` waiting entries + `pr_view` `mergeEtaSim`), `server/api/pipeline.py` (`_build_state` passes sim inputs)
- Test: `server/tests/test_merge_sim.py`, extend `server/tests/test_dashboard_state.py`

**Interfaces:**
- Consumes: Task 2's `group_run_samples`/`count_group_runs`/`count_group_ejects`; queue entries.
- Produces: pure function (SPEC §1 — plotroom estimator/queue.ts:107-167 semantics):

```python
EJECT_BUMP_MIN_PROB = 0.15
EJECT_PROB_MIN_SAMPLES = 5

def eject_probability(runs_7d: int, ejects_7d: int) -> float:
    total = runs_7d + ejects_7d
    if total < EJECT_PROB_MIN_SAMPLES:
        return 0.0
    return ejects_7d / total

def simulate_merge_eta(
    queued_ahead: int, batch_size: int, duration_samples: list[int],
    eject_prob: float, current_train_eta_secs: int | None,
) -> dict | None:
    """Plotroom queue.ts simulateMergeEta: analytic multi-train ETA for a
    WAITING entry. Returns MergeEtaSimulation dict or None without samples."""
    if not duration_samples:
        return None
    s = sorted(duration_samples)
    dur50 = s[len(s) // 2]
    dur90 = s[min(len(s) - 1, int(len(s) * 0.9))]
    import math
    trains = math.ceil((queued_ahead + 1) / max(1, batch_size))
    bump = 1 if eject_prob > EJECT_BUMP_MIN_PROB else 0
    base = current_train_eta_secs or 0
    return {
        "p50Secs": round(base + trains * dur50),
        "p90Secs": round(base + (trains + bump) * dur90),
        "trainsAhead": (trains - 1) + (1 if current_train_eta_secs is not None else 0),
        "assumesEjects": bump == 1,
    }
```
(Percentile indexing: match plotroom's exact p50/p90 picks — read SPEC §1; if it specifies interpolation, mirror it and pin with a test using a known 20-sample array.)
- Wiring: for each WAITING entry (not building/covered, not UNMERGEABLE/MERGEABLE — SPEC §1 edge cases): `queued_ahead` = count of waiting entries at lower position; `current_train_eta_secs` = deepest building group's etaSeconds (or None); sim lands on `queue_view.waiting[].sim` AND the corresponding `pr_view.mergeEtaSim` (waiting queue PRs only).

- [ ] **Step 1: Failing unit tests** — no samples → None; single train, no eject bump; eject bump above 15%; trainsAhead with/without building train; UNMERGEABLE/MERGEABLE excluded (wiring test); the plotroom p50/p90 pick pinned on a fixed 20-sample array.
- [ ] **Step 2: Implement + wire.** `_build_state` gathers per-repo `duration_samples`/eject-prob once per frame (async store reads; keep it one query per repo, not per PR).
- [ ] **Step 3: RED→GREEN; extend test_dashboard_state with a waiting-entry sim assertion; SSE test still green.**
- [ ] **Step 4: Gates + commit** — `feat(pipeline): mergeEtaSim — multi-train merge ETA simulation (plotroom queue.ts port)`.

---

### Task 4: PrTimeline capture

**Files:**
- Create: `server/pipeline/timeline.py` (store: `upsert_merged_pr(...)` with COALESCE-style first-value-wins, `mark_env_live(repo, number, env, at_iso)` writing qa_live_at/prod_live_at only when NULL, `timelines_for(repo, numbers) -> dict[int, dict]`, `tracked_merged(repo, retention_days, now) -> list[MergedPr]`)
- Modify: `server/github/pr_status.py` (add `createdAt` + `mergedAt` already present? — verify `_PR_NODE_FIELDS` at pr_status.py:221; add `createdAt` if missing and thread `created_at` onto `PullRequestInfo` as a nullable field with to_dict/from_dict round-trip), `server/github/pr_population.py` (transition hooks)
- Test: `server/tests/test_timeline.py`

**Interfaces:**
- Consumes: Task 1 `MergedPr`; population poller cycle state.
- Produces (consumed by Tasks 5-6): the store functions above. Capture semantics (SPEC §2):
  - **first_green_at**: in the population cycle, track per-(repo, number) the previous stage; when an open PR transitions `ci` → `ready`|`queue` for the first time, remember `now` in an in-memory map; consumed at merge.
  - **enqueued_at**: while a PR is observed in the queue (QUEUED|AWAITING_CHECKS), cache the queue observation's `enqueued_at` (epoch → ISO).
  - **on merge observed** (PR newly `merged` in the population): `upsert_merged_pr(repo, number, title, url, merged_at, merge_commit_sha, created_at, first_green_at?, enqueued_at?)` — first value wins for every nullable column; in-memory maps cleaned up.
  - `merge_commit_sha`: check whether the population GraphQL already fetches `mergedAt`/`mergeCommitSha` (grep `_PR_NODE_FIELDS`); add `mergeCommitOid`... GitHub GraphQL field is `mergeCommit { oid }` — add it alongside `createdAt`.
- **Payload**: `pr_view` gains `timeline` for merged PRs (Task 6 wires it; keep this task capture-only).

- [ ] **Step 1: Failing store tests** — upsert twice (first-wins), mark_env_live idempotent (only writes NULL→value), tracked_merged respects retention window (default 7 days), timelines_for returns camelCase-ready dicts `{createdAt, firstGreenAt, enqueuedAt, mergedAt, qaLiveAt, prodLiveAt}`.
- [ ] **Step 2: GraphQL additions** — `createdAt` + `mergeCommit { oid }` in `_PR_NODE_FIELDS` (BOTH the per-branch and population queries share it — that's the point of the shared constant); thread through `_to_info`, `to_dict`/`from_dict` (nullable, default None/""). Update any query-shape fixture tests.
- [ ] **Step 3: Poller hooks** — transition detection + merge upsert in the population cycle (wrap in try/except + log; never break the poll). Integration test: two synthetic cycles (open-ci → merged) produce a MergedPr row with first_green_at set when green was observed in between.
- [ ] **Step 4: Gates + commit** — `feat(pipeline): PrTimeline capture — created/first-green/enqueued/merged waypoints`.

---

### Task 5: Deploy environments — config, health poller, ancestry, qa/prod-live

**Files:**
- Create: `server/pipeline/deploy_watch.py`
- Modify: `server/sessions/orchestrator.py` (expose `repo_deploy_status()`; start/stop the watcher alongside existing pollers — find how pr_population's poller is started, mirror it), RemoteOrchestrator + RPC + facade (three one-liners; the reflection guard catches the facade half)
- Test: `server/tests/test_deploy_watch.py`

**Interfaces:**
- Consumes: Task 1 `RepoDeployConfig`, Task 4 store (`mark_env_live`, `tracked_merged`).
- Produces (consumed by Task 6): `DeployWatcher` with:
  - `poll_once(now) -> None`: for each enabled RepoDeployConfig env → `GET healthUrl` (httpx, 5 s timeout) → extract `shaKey` (default `"commitSha"`) from JSON → cache `{(repo, env): {"liveSha": str|None, "reachable": bool}}`. Failures → liveSha None, reachable False — never raises.
  - Ancestry: for each tracked merged PR (retention window) with a merge_commit_sha and un-set qa/prod_live_at: `is_ancestor(repo, sha, live_sha)` via GitHub compare API — `gh api repos/{repo}/compare/{sha}...{live_sha}` → status in ("identical", "ahead") means sha is an ancestor of live_sha... VERIFY the direction: compare `base...head` status "ahead" = head ahead of base = base is ancestor. Pin with a unit test against a faked gh runner; throttle re-checks per (sha, liveSha) pair (in-memory set, SPEC §3).
  - On ancestor-yes: `mark_env_live(repo, number, env_name, now_iso)`; env named `prod`/`production` writes prod_live_at, anything else writes qa_live_at (plotroom names envs 'qa'/'prod'; ccswitch config uses the configured name — map: env.name in ("prod", "production") → prod, else qa).
  - `status() -> dict[str, dict]`: `{repo: {"envs": [{"name","liveSha","reachable"}], ...}}` (raw cache for Task 6).
  - Poll cadence 30 s (`_DEPLOY_INTERVAL_S = 30.0`, module constant; Phase 4 makes it a setting).
- API for config (minimal Phase 3): `GET/PUT /api/pipeline/deploy-config` (list all rows / upsert one row `{repo, environments: [...]}`, identity-gated, pydantic-validated). This is how the user configures envs until the Phase-4 settings panel.

- [ ] **Step 1: Failing tests** — health extraction (nested shaKey path? SPEC §3 says key in JSON response — flat key lookup; non-string → None), unreachable → reachable False; ancestry direction pinned against a fake gh runner returning each compare status; mark-env-live wiring (qa vs prod name mapping); config API roundtrip (temp-file DB).
- [ ] **Step 2: Implement watcher + API + orchestrator accessor + proxy/RPC/facade.**
- [ ] **Step 3: RED→GREEN + facade guard + breadth test green.**
- [ ] **Step 4: Gates + commit** — `feat(pipeline): deploy environment watcher — health polling, ancestry, qa/prod-live marking`.

---

### Task 6: computeRepoDeploy + dashboard wiring (deploy block, timeline, hasDeploy)

**Files:**
- Create: `server/pipeline/deploy_status.py`
- Modify: `server/pipeline/dashboard_state.py` + `server/api/pipeline.py` (`_build_state`)
- Test: `server/tests/test_deploy_status.py`, extend `server/tests/test_dashboard_state.py`

**Interfaces:**
- Consumes: Task 4 `tracked_merged`/`timelines_for`, Task 5 `DeployWatcher.status()`.
- Produces: `compute_repo_deploy(merges: list[MergedPr], envs: list[dict], chain_limit: int = 8) -> dict` — SPEC §3 exactly:

```python
def compute_repo_deploy(merges, envs, chain_limit=8):
    """plotroom deploy-status.ts computeRepoDeploy + buildChain (SPEC §3).
    merges: MergedPr rows in the retention window, any order."""
    ordered = sorted(merges, key=lambda m: m.merged_at, reverse=True)
    newest_prod = next((m for m in ordered if m.prod_live_at), None)
    awaiting_qa = awaiting_prod = 0
    for m in ordered:
        if m.prod_live_at:
            continue
        if newest_prod and m.merged_at < newest_prod.merged_at:
            continue                        # superseded (#205): rolled into a newer prod deploy
        if m.qa_live_at:
            awaiting_prod += 1
        else:
            awaiting_qa += 1
    entries = []
    for m in ordered[:chain_limit]:
        stage = "prod" if m.prod_live_at else "qa" if m.qa_live_at else "merged"
        superseded = (not m.prod_live_at and newest_prod is not None
                      and newest_prod.merged_at > m.merged_at)
        entries.append({"prNumber": m.number, "sha": m.merge_commit_sha,
                        "mergedAt": m.merged_at, "stage": stage,
                        "qaLiveAt": m.qa_live_at, "prodLiveAt": m.prod_live_at,
                        "superseded": superseded})
    in_flight = next(({"prNumber": e["prNumber"], "sha": e["sha"], "stage": e["stage"]}
                      for e in entries if e["stage"] != "prod" and not e["superseded"]), None)
    return {"envs": envs, "awaitingQa": awaiting_qa, "awaitingProd": awaiting_prod,
            "chain": {"entries": entries, "inFlight": in_flight,
                      "supersededCount": sum(1 for e in entries if e["superseded"])}}
```
- Wiring in `_build_state`/`dashboard_state`: repos with a RepoDeployConfig get `repo["deploy"] = compute_repo_deploy(...)` and `hasDeploy=True` (config presence now drives hasDeploy alongside the stage-model derivation — config wins); `pr_view` gains `timeline=timelines_for(...)` for merged PRs (dict or None). `stage_result` for merged PRs can now use real qa/prod facts: merged + config repo + qa_live_at set + prod_live_at unset → stage 'awaiting-prod'; merged + neither → 'merged' with the deploy block carrying the truth (keep the existing deployed_envs fallback for non-configured repos, documented).

- [ ] **Step 1: Failing tests** — the SPEC §3 scenarios: PR at each stage; the supersession case (A merges, B merges later and reaches prod → A superseded, excluded from both awaiting counters, chain marks it); inFlight selection; chain limit; empty merges.
- [ ] **Step 2: Implement + wire (timeline + deploy + hasDeploy).**
- [ ] **Step 3: RED→GREEN; dashboard/SSE suites green.**
- [ ] **Step 4: Gates + commit** — `feat(pipeline): deploy status + supersession chain + live PrTimeline in the SSE payload`.

---

### Task 7: touchesWorkflows + workflowImpact

**Files:**
- Create: `server/pipeline/ci_graph.py` (derive + diff), `server/pipeline/workflow_impact.py` (cache + orchestration)
- Modify: `server/github/pr_status.py` (`files(first: 100) { nodes { path } }` in `_PR_NODE_FIELDS` → `touches_workflows: bool` on PullRequestInfo, nullable-safe), `server/pipeline/dashboard_state.py` (`pr_view` emits both fields)
- Test: `server/tests/test_ci_graph.py`, `server/tests/test_workflow_impact.py`

**Interfaces:**
- Consumes: history store's ciGraph meta (`prefixes`, `workflowName` — history.py:299-314) for the MAIN-branch graph; gh helper to fetch workflow YAML blobs (`gh api repos/{repo}/contents/.github/workflows/{file}?ref={sha}` — base64).
- Produces:
  - `derive_ci_graph(yaml_text: str, rollup_job_id: str) -> dict | None` — parse workflow YAML; BFS the `needs:` closure from `rollup_job_id`; return `{"prefixes": set[str], "nodes": {job_id: {"timeoutMinutes": int|None}}}`; None on parse failure.
  - `diff_ci_graphs(base: dict, head: dict) -> dict | None` — SPEC §4 exactly: `± <prefix> joins the merge_group gate` / `− <prefix> leaves the merge_group gate` / `required-check set {grows|shrinks} by N: A → B checks` / `<prefix> timeout-minutes Xm → Ym` (null → `unset`); None when no lines.
  - `workflow_impact_for(repo, head_sha) -> dict | None` — resolve the workflow FILE: list `.github/workflows/` at main, pick the file whose parsed `name:` equals the ciGraph meta `workflowName` (cache per repo); fetch that file at main and at head_sha; derive both graphs (rollup job id = the ciGraph's rollup — check how `required.py` stores/derives it; if only prefixes are stored, derive rollup job id from meta or accept the workflowName-rooted whole-graph closure — document the choice); diff. In-memory cache keyed `(repo, head_sha)`; evict keys for PRs no longer open each cycle. Any failure → None (honest null).
- `touchesWorkflows`: any changed-file path starting `.github/workflows/` (files page 1 only — 100 files; PRs with >100 files may miss it — document; plotroom has the same page-1 behavior).
- Impact computed only when `touches_workflows` and head_sha known.

- [ ] **Step 1: Failing tests** — derive: a small workflow YAML with needs chains + timeout-minutes; BFS closure correct; malformed YAML → None. diff: each summary line type pinned VERBATIM (copy expected strings from SPEC §4, including the `+`/`−`/`±` glyphs — read SPEC §4 carefully: joins is `± ` or `+ `? Use the SPEC's exact rendering and pin it). files→flag mapping.
- [ ] **Step 2: Implement + wire** (GraphQL files field, PullRequestInfo threading with dict round-trip, pr_view emission, per-cycle cache eviction).
- [ ] **Step 3: RED→GREEN; population/query-shape tests updated.**
- [ ] **Step 4: Gates + commit** — `feat(pipeline): touchesWorkflows + CI-graph workflow-impact diffs`.

---

### Task 8: Parity phase 3

**Files:**
- Modify: `server/github/parity/coverage.py` (+ tests `server/tests/test_parity_coverage.py`)
- Test: extend as below

**Interfaces:** `PHASE_FIELDS[3] = PHASE_FIELDS[2] | {"touches_workflows"}` — the only new field the oracle (plotroom `/api/state`) exposes per-PR that ccswitch now computes AND that survives normalize.py's mapping (timeline/mergeEtaSim/deploy are structural payloads, not per-PR scalar fields the harness compares; note that in a comment). Update `normalize.py` to map the oracle's `touchesWorkflows` → `touches_workflows`, and the candidate computation to include it.

- [ ] **Step 1: Failing test** — `test_phase3_adds_touches_workflows()` (mirror the phase-1 test at test_parity_coverage.py) + an `harness`-level test that a `touches_workflows` mismatch is detected at phase 3 and ignored at phase 2.
- [ ] **Step 2: Implement; run the parity unit suites** (`test_parity_coverage.py`, `test_parity_harness.py`).
- [ ] **Step 3: Gates + commit** — `feat(parity): phase 3 — touches_workflows coverage`.

---

### Task 9: Full verification + PR

- [ ] **Step 1: Full server suite** (Global-Constraints command) + full web gates (`npx vitest run && npx tsc --noEmit && npx eslint src && npm run build` — expect zero web changes; if any web test broke, something leaked).
- [ ] **Step 2: Live smoke if practical:** with a configured RepoDeployConfig row and real polling, `curl -N localhost:PORT/api/pipeline/events | head` shows `mergeEtaSim`/`timeline`/`deploy` populating (or note why not run).
- [ ] **Step 3: Push + PR + auto-merge:**
```bash
git push -u origin feat/plotroom-pipeline-phase3
gh pr create --title "feat(pipeline): plotroom pipeline replica — Phase 3 data gap-fill" --body "$(cat <<'EOF'
Phase 3 of the plotroom exact-replica (spec §4/§8): mergeEtaSim (multi-train merge ETA from
recorded group-run durations + eject probability), PrTimeline capture (created/first-green/
enqueued/merged/qa-live/prod-live → live Waterfall), deploy environment tracking (per-repo
health-URL config + polling, GitHub-compare ancestry, awaiting-QA/prod + supersession chain),
and touchesWorkflows + CI-graph workflowImpact diffs. Parity phase 3 added. No frontend
changes — the Phase-1 ported components render all of it as-is.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

---

## Self-Review Notes

- **Spec §8 phase-3 coverage:** mergeEtaSim (T2 substrate + T3), PrTimeline + Waterfall (T4 + T6 wiring), deploy envs + chain (T5 + T6), workflow-impact (T7). Parity (T8).
- **Deviations from plotroom, all declared:** ancestry via GitHub compare API instead of local clones (no cloneUrl config needed in Phase 3); deploy env config in a DB table + minimal API instead of config.json (single source of truth; Phase-4 settings panel edits it); qa/prod mapping by env name; files page-1 cap matches plotroom.
- **Type consistency:** `simulate_merge_eta` returns the exact `MergeEtaSimulation` camelCase dict; `timelines_for` returns the exact `PrTimeline` camelCase dict; `compute_repo_deploy` returns the exact `repos[].deploy` shape from types.ts:156-161.
- **Placeholder scan:** clean — every algorithm step carries code or a SPEC § reference with exact semantics; the two "verify direction/rendering" notes instruct pinning against SPEC/API truth with tests, not deferring the work.

# M-Track-7: Reorder Merged before deploy envs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorder the pipeline stage track from `CI→Ready→[Queue]→deploy:envs→Merged` to `CI→Ready→[Queue]→Merged→deploy:envs`, redefine the Merged node as "done when merged" (not "live everywhere"), and reorder the metric tiles to match.

**Architecture:** Two pure modules change. `server/pipeline/track.py`: `build_stage_model` emits the `merged` node before the deploy nodes; `place_pr_on_track`'s merged-PR branch makes `merged` a plain `done` pre-deploy node (dropping the terminal/frontier special-case), with the per-env deploy frontier unchanged and relocated after it. `web/src/session/stageBucket.ts`: reorder the `STAGE_BUCKETS` constant so the tiles read CI·Ready·Queue·Merged·Deploy.

**Tech Stack:** Python 3.11 / pytest (backend); React + TypeScript / vitest (frontend).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-30-visual-pipeline-track-m7-merged-before-deploy-design.md`.
- **New node order:** `ci, ready, [queue], merged, deploy:<env>…` (merged BEFORE deploys).
- **Merged node = done when `pr.stage == "merged"`** — a plain pre-deploy `done` node; the old "done only when live in every env" / M-Track-1 "active terminal" special-cases are REMOVED.
- **Deploy nodes keep the existing per-env frontier:** `done` if `env ∈ deployed` (where `deployed = set(pr.deployed_envs) ∪ ({"production"} if pr.deployed else ∅)`), `active` if it's the frontier (first non-deployed deploy node, in order, after the furthest deployed), else `pending`. Logic unchanged, just relocated after `merged`.
- **No-deploy-env repo:** a merged PR's `merged` node renders `done` (all green).
- **Non-merged PRs:** placement UNCHANGED (forward cursor by `pr.stage`; merged + deploy nodes `pending`).
- **Tiles:** `STAGE_BUCKETS = ['ci','ready','queue','merged','deploy']`. `STAGE_BUCKET_LABELS`, `KIND_TO_BUCKET`, `stageBucketOf` UNCHANGED.
- **Intended test rewrites:** the M-Track-2 placement assertions (`merged: pending`/`active`) and the `stageBucket.test.ts` order assertion change deliberately to the new semantics — they are NOT regressions. Any OTHER test that changes is a real regression to investigate.
- **No signature change** — `build_stage_model`/`place_pr_on_track`/`stageBucketOf` keep their shapes; no caller fan-out. `place_pr_on_track` is consumed by `all_prs`, so run the whole backend track/pipeline suite.
- **Backend gate:** `ruff` + `mypy` + `pytest`, via `/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/42659d9c/.venv/bin/python`; auth tests prefix `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. **Frontend gate (in `web/`):** `npx vitest run`, `npx tsc -b --force`, `npx eslint . --max-warnings 0`.

---

### Task 1: Reorder Merged in the track (backend)

**Files:**
- Modify: `server/pipeline/track.py` (`build_stage_model` specs ~95-101; `place_pr_on_track` merged branch ~119-161)
- Test: `server/tests/test_build_stage_model.py` (update order assertions), `server/tests/test_place_pr_on_track.py` (update merged-PR assertions)

**Interfaces:**
- Consumes: `StageModel`, `StageNode` (kinds `ci|gate|queue|deploy|merged`), `NodeStatus`, `PullRequestInfo` (`stage`, `deployed`, `deployed_envs`).
- Produces: `build_stage_model(...)` node order `ci,ready,[queue],merged,deploy:<env>…`; `place_pr_on_track(pr, model) -> list[NodeStatus]` with merged node `done` for merged PRs.

- [ ] **Step 1: Update the build_stage_model order tests (RED)**

In `server/tests/test_build_stage_model.py`, change the deploy-bearing assertions to put `merged` before deploys:
- `test_deploy_node_only_when_has_deploy`:
```python
def test_deploy_node_only_when_has_deploy():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["production"])
    assert _ids(m) == ["ci", "ready", "merged", "deploy:production"]
    assert m.nodes[3].kind == "deploy"
    assert m.nodes[3].label == "production"
```
- `test_full_track_order`:
```python
def test_full_track_order():
    m = build_stage_model("o/r", ci_graph={"workflowName": "CI"}, has_queue=True, deploy_envs=["production"])
    assert _ids(m) == ["ci", "ready", "queue", "merged", "deploy:production"]
    assert [n.order for n in m.nodes] == [0, 1, 2, 3, 4]
```
- `test_multiple_deploy_nodes_in_given_order`:
```python
def test_multiple_deploy_nodes_in_given_order():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "staging", "production"])
    ids = [n.id for n in m.nodes]
    assert ids == ["ci", "ready", "merged", "deploy:qa", "deploy:staging", "deploy:production"]
    assert [n.label for n in m.nodes if n.kind == "deploy"] == ["qa", "staging", "production"]
    assert [n.order for n in m.nodes] == [0, 1, 2, 3, 4, 5]
```
(Leave `test_minimal_repo_collapses_to_ci_ready_merged`, `test_queue_node_only_when_has_queue`, `test_empty_deploy_envs_no_deploy_nodes`, `test_ci_label_from_cigraph_workflow_name` unchanged — no deploy nodes, so order is already correct.)

- [ ] **Step 2: Update the place_pr_on_track merged-PR tests (RED)**

In `server/tests/test_place_pr_on_track.py`, the merged node flips to `done` for merged PRs; deploy-node states are unchanged:
- `test_merged_not_deployed_leaves_deploy_active` → add merged-done assertion:
```python
def test_merged_not_deployed_leaves_deploy_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["production"])
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=False), m)
    assert st["deploy:production"] == "active"
    assert st["merged"] == "done"
```
- `test_merged_pr_no_deploy_node_merged_active` (rename intent: now done) →
```python
def test_merged_pr_no_deploy_node_merged_done():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    st = _states(_pr(stage="merged", state="merged", merged=True, deployed=False), m)
    assert st == {"ci": "done", "ready": "done", "merged": "done"}
```
- `test_merged_live_in_first_env_only`:
```python
def test_merged_live_in_first_env_only():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "staging", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=("qa",))
    st = _states(pr, m)
    assert st == {"ci": "done", "ready": "done", "merged": "done", "deploy:qa": "done",
                  "deploy:staging": "active", "deploy:production": "pending"}
```
- `test_merged_live_in_none_first_deploy_active`:
```python
def test_merged_live_in_none_first_deploy_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["qa", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=())
    st = _states(pr, m)
    assert st["deploy:qa"] == "active"
    assert st["deploy:production"] == "pending"
    assert st["merged"] == "done"
```
- `test_merged_skipped_env_no_active_behind_done`:
```python
def test_merged_skipped_env_no_active_behind_done():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "staging", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=("production",))
    st = _states(pr, m)
    assert st == {"ci": "done", "ready": "done", "merged": "done", "deploy:qa": "pending",
                  "deploy:staging": "pending", "deploy:production": "done"}
```
(`test_merged_pr_all_done` and `test_merged_live_in_all_envs_merged_done` already assert all-`done` → unchanged. The non-merged tests `test_ci_pending_pr_lights_ci_active`, `test_ready_pr_ci_done_ready_active`, `test_queue_pr_*`, `test_parked_failing_pr_lights_ci_failed` are unchanged — no deploy nodes / not merged.)

- [ ] **Step 3: Run to verify RED**

Run: `.venv/bin/python -m pytest server/tests/test_build_stage_model.py server/tests/test_place_pr_on_track.py -q`
Expected: FAIL — order still puts deploy before merged; merged still pending/active.

- [ ] **Step 4: Reorder the node spec in `build_stage_model`**

In `server/pipeline/track.py`, move the `merged` spec ABOVE the deploy loop:
```python
    specs.append(("ci", ci_label, "ci"))
    specs.append(("ready", "Ready", "gate"))
    if has_queue:
        specs.append(("queue", "Queue", "queue"))
    specs.append(("merged", "Merged", "merged"))
    for env in deploy_envs:
        specs.append((f"deploy:{env}", env, "deploy"))
```

- [ ] **Step 5: Simplify the merged-PR branch in `place_pr_on_track`**

Replace the merged branch (the `if pr.stage == "merged":` block, ~119-161) with:
```python
    if pr.stage == "merged":
        # A merged PR has landed: ci/ready/queue/merged are all done. Deploy nodes
        # (now AFTER merged) carry the promotion frontier.
        deployed = set(getattr(pr, "deployed_envs", ()) or ())
        if pr.deployed:  # back-compat: deployed=True without deployed_envs
            deployed.add("production")

        deploy_nodes = [n for n in model.nodes if n.kind == "deploy"]
        last_deployed_order = max(
            (n.order for n in deploy_nodes if n.id.split("deploy:", 1)[1] in deployed),
            default=-1,
        )
        frontier_id: str | None = None
        for n in deploy_nodes:
            env = n.id.split("deploy:", 1)[1]
            if env not in deployed and n.order > last_deployed_order:
                frontier_id = n.id
                break

        out: list[NodeStatus] = []
        for n in model.nodes:
            if n.kind == "deploy":
                env = n.id.split("deploy:", 1)[1]
                if env in deployed:
                    state = "done"
                elif n.id == frontier_id:
                    state = "active"
                else:
                    state = "pending"
            else:  # ci / ready / queue / merged — all cleared by a merged PR
                state = "done"
            out.append(NodeStatus(id=n.id, state=state))
        return out
```
(The `has_deploy_nodes` variable and the `elif n.kind == "merged":` special-case are deleted — `merged` now falls into the `else` → `done`. The non-merged branch below is unchanged.)

- [ ] **Step 6: Run to verify GREEN**

Run: `.venv/bin/python -m pytest server/tests/test_build_stage_model.py server/tests/test_place_pr_on_track.py -q`
Expected: PASS.

- [ ] **Step 7: Lint/type + the whole track/pipeline suite (place_pr_on_track feeds all_prs)**

```bash
.venv/bin/python -m ruff check server/pipeline/track.py
.venv/bin/python -m mypy server/pipeline/track.py   # ignore pre-existing ccswitch import-untyped
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest \
  server/tests/test_build_stage_model.py server/tests/test_place_pr_on_track.py \
  server/tests/test_orchestrator_stage_models.py server/tests/test_track_model.py \
  server/tests/test_stage_models_rpc_roundtrip.py server/tests/test_deployed_envs.py -q
```
Expected: all PASS. If any assertion OTHER than merged-ordering/semantics changed, STOP — that's a real regression.

- [ ] **Step 8: Commit**

```bash
git add server/pipeline/track.py server/tests/test_build_stage_model.py server/tests/test_place_pr_on_track.py
git commit -m "feat(pipeline): order Merged before deploy envs; Merged=done-when-merged"
```

---

### Task 2: Reorder the metric tiles (frontend)

**Files:**
- Modify: `web/src/session/stageBucket.ts` (the `STAGE_BUCKETS` constant)
- Test: `web/src/session/__tests__/stageBucket.test.ts` (order assertion), `web/src/components/__tests__/StageTiles.test.tsx` (tile render order)

**Interfaces:**
- Consumes: `stageBucketOf`, `STAGE_BUCKETS`, `STAGE_BUCKET_LABELS` (this module); `StageTiles` renders in `STAGE_BUCKETS` order.
- Produces: `STAGE_BUCKETS = ['ci','ready','queue','merged','deploy']`.

- [ ] **Step 1: Update the order assertion (RED)**

In `web/src/session/__tests__/stageBucket.test.ts`, change the `STAGE_BUCKETS` order assertion:
```ts
  it('exposes the fixed bucket order', () => {
    expect(STAGE_BUCKETS).toEqual(['ci', 'ready', 'queue', 'merged', 'deploy'])
  })
```

- [ ] **Step 2: Add a tile-order assertion to StageTiles (RED)**

In `web/src/components/__tests__/StageTiles.test.tsx`, add:
```tsx
it('renders tiles in CI · Ready · Queue · Merged · Deploy order', () => {
  render(<StageTiles prs={[]} stageModels={{}} active={new Set()} onToggle={() => {}} />)
  const order = screen.getAllByTestId(/^stage-tile-/).map((el) => el.getAttribute('data-testid'))
  expect(order).toEqual([
    'stage-tile-ci', 'stage-tile-ready', 'stage-tile-queue', 'stage-tile-merged', 'stage-tile-deploy',
  ])
})
```
(Ensure `screen` is imported in that test file; it already uses `@testing-library/react`.)

- [ ] **Step 3: Run to verify RED**

Run (in `web/`): `npx vitest run src/session/__tests__/stageBucket.test.ts src/components/__tests__/StageTiles.test.tsx`
Expected: FAIL — order is still `[…,'deploy','merged']`.

- [ ] **Step 4: Reorder `STAGE_BUCKETS`**

In `web/src/session/stageBucket.ts`:
```ts
export const STAGE_BUCKETS: StageBucket[] = ['ci', 'ready', 'queue', 'merged', 'deploy']
```
(Nothing else changes — `STAGE_BUCKET_LABELS`, `KIND_TO_BUCKET`, and `stageBucketOf` are untouched.)

- [ ] **Step 5: Run to verify GREEN + full frontend gate**

Run (in `web/`): `npx vitest run && npx tsc -b --force && npx eslint . --max-warnings 0`
Expected: all green (the existing `stageBucketOf` behaviour tests still pass — only the order constant changed).

- [ ] **Step 6: Commit**

```bash
git add web/src/session/stageBucket.ts web/src/session/__tests__/stageBucket.test.ts web/src/components/__tests__/StageTiles.test.tsx
git commit -m "feat(web): reorder stage tiles to CI·Ready·Queue·Merged·Deploy"
```

---

## Self-Review

**Spec coverage:**
- Node reorder (merged before deploys) → Task 1 Step 4. ✅
- Merged = done-when-merged; drop terminal/frontier special-case → Task 1 Step 5. ✅
- Deploy frontier unchanged, relocated → Task 1 Step 5 (same `last_deployed_order`/`frontier_id` logic). ✅
- No-env repo merged → done → Task 1 Step 2 (`test_merged_pr_no_deploy_node_merged_done`) + Step 5 (`else → done`). ✅
- Non-merged unchanged → Task 1 (non-merged branch untouched; those tests unchanged). ✅
- Tiles reorder → Task 2. ✅
- Intended test rewrites flagged → Global Constraints + Task 1 Steps 1-2, Task 2 Steps 1-2. ✅
- Whole-suite for place_pr_on_track (feeds all_prs) → Task 1 Step 7. ✅

**Placeholder scan:** none — every step has concrete code/commands and exact expected node-id lists and state dicts.

**Type consistency:** `STAGE_BUCKETS` value `['ci','ready','queue','merged','deploy']` is identical in Task 2 Step 4 and both its test assertions; the node-order `['ci','ready','queue','merged','deploy:<env>…']` is identical across Task 1's build_stage_model edit and its order tests; the merged-branch `else → done` matches every updated `merged: done` placement assertion; deploy-node frontier states (done/active/pending) are unchanged from the current code and match the updated tests.

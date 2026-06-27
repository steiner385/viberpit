# Visual Pipeline Track — M-Track-2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render one ordered `deploy:<env>` node per real GitHub deployment environment (dev/qa → staging → production) instead of M-Track-1's single hardcoded `deploy:production`, with each node's state derived from whether the PR's merge is live in that environment.

**Architecture:** Backend-only (the dumb `StageTrack` already renders whatever nodes the backend emits). The per-repo population query fetches un-filtered deployments tagged by `latestEnvironment`; `_active_deploy_dates` computes a newest-SUCCESS date per env; `PullRequestInfo.deployed_envs` records the envs a merged PR is live in; `order_deploy_envs` ranks envs conventionally; `build_stage_model` takes `deploy_envs: list[str]`; `place_pr_on_track` walks multiple deploy nodes via `deployed_envs`. Orchestrator derives each repo's env set as the union of its PRs' `deployed_envs` (Option A — no poller change).

**Tech Stack:** Python 3 / FastAPI / dataclasses / pytest (backend); React + TS + vitest (one frontend test).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-27-visual-pipeline-track-m2-design.md`. Base branch `feat/visual-pipeline-track-m2` (off `origin/master`, which already has M-Track-1).
- `Deployment.latestEnvironment` is verified present on the live GitHub schema (probed 2026-06-27).
- **Option A:** deploy node set = union of per-PR `deployed_envs`. Do NOT add a GitHub `environments` enumeration and do NOT change the poller cache.
- **Back-compat:** `deployed_envs` defaults `()`; the `deployed` bool is redefined as `"production" in deployed_envs`. `place_pr_on_track` bridges: `deployed = set(pr.deployed_envs or ()); if pr.deployed: deployed.add("production")` — this keeps M-Track-1's `deployed=True` tests green. Serialization additive (no migration). Assert on real responses (memory: pullrequestinfo-todict-exact-shape-test, session-record-dual-serializer).
- Preserve M-Track-1 behavior exactly: a merged PR in a model with **no** deploy nodes → `merged` node `active` (not done).
- Reuse `stage.py` as the cursor; no stage re-derivation (constitution VI).
- Test env (from worktree root): `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …`; API-auth tests prefix `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`. Frontend in `web/`: `npm ci` first (fresh worktree). Real gates: full `pytest`, `ruff`, `mypy`; full `vitest run`, `tsc -b --force`, `eslint --max-warnings 0`. Judge backend regressions by the baseline-diff technique (memory: worktree-fullsuite-baseline-diff), not raw counts.

---

### Task 1: `order_deploy_envs` env-ordering helper

**Files:**
- Modify: `server/pipeline/track.py`
- Test: `server/tests/test_order_deploy_envs.py`

**Interfaces:**
- Produces: `order_deploy_envs(envs: Iterable[str]) -> list[str]` — deterministic conventional order.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_order_deploy_envs.py
from server.pipeline.track import order_deploy_envs


def test_conventional_order_dev_qa_staging_prod():
    assert order_deploy_envs(["production", "staging", "qa", "dev"]) == ["dev", "qa", "staging", "production"]


def test_unknown_envs_cluster_middle_alphabetical():
    # unknown rank == staging rank (2); alphabetical tie-break among rank-2 items
    out = order_deploy_envs(["production", "zebra", "alpha", "dev"])
    assert out[0] == "dev"
    assert out[-1] == "production"
    assert out.index("alpha") < out.index("zebra")


def test_prod_family_always_last():
    assert order_deploy_envs(["live", "qa"])[-1] == "live"
    assert order_deploy_envs(["release-prod", "test"])[-1] == "release-prod"


def test_empty():
    assert order_deploy_envs([]) == []


def test_dedup_preserves_single():
    assert order_deploy_envs(["qa", "qa"]) == ["qa"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_order_deploy_envs.py -v`
Expected: FAIL `ImportError: cannot import name 'order_deploy_envs'`

- [ ] **Step 3: Write minimal implementation** (append to `server/pipeline/track.py`; add `from collections.abc import Iterable` near the top imports)

```python
# Conventional deploy-environment ordering. Lower rank = earlier in the pipeline.
_ENV_RANK = [
    (("dev", "development"), 0),
    (("test", "qa", "integration"), 1),
    (("stage", "staging", "preprod", "uat", "sandbox"), 2),
    (("prod", "production", "live", "release"), 3),
]


def _env_rank(env: str) -> int:
    low = env.lower()
    for keywords, rank in _ENV_RANK:
        if any(k in low for k in keywords):
            return rank
    return 2  # unknown envs cluster with staging-tier, alphabetical tie-break


def order_deploy_envs(envs: "Iterable[str]") -> list[str]:
    """Deterministic conventional ordering: dev/qa → staging/unknown → prod, alpha tie-break."""
    uniq = sorted(set(envs))
    return sorted(uniq, key=lambda e: (_env_rank(e), e))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_order_deploy_envs.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_order_deploy_envs.py
git commit -m "feat(pipeline): order_deploy_envs conventional env ordering"
```

---

### Task 2: `build_stage_model` takes `deploy_envs: list[str]`

**Files:**
- Modify: `server/pipeline/track.py:61-82` (`build_stage_model`)
- Modify: `server/tests/test_build_stage_model.py` (existing M-Track-1 tests use `has_deploy=`)
- Test: same file (add multi-deploy cases)

**Interfaces:**
- Produces: `build_stage_model(repo, *, ci_graph, has_queue, deploy_envs: list[str]) -> StageModel` — one `deploy:<env>` node (kind `"deploy"`, label = env) per env in `deploy_envs` order, between `queue` and `merged`.

- [ ] **Step 1: Update the failing tests**

In `server/tests/test_build_stage_model.py`, replace every `has_deploy=False` with `deploy_envs=[]` and every `has_deploy=True` with `deploy_envs=["production"]`. The existing `test_deploy_node_only_when_has_deploy` asserts `["ci","ready","deploy:production","merged"]` — keep it (now via `deploy_envs=["production"]`), and the node's label is now `"production"` (was `"Deploy"`) — assert `m.nodes[2].label == "production"`. Then add:

```python
def test_multiple_deploy_nodes_in_given_order():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "staging", "production"])
    ids = [n.id for n in m.nodes]
    assert ids == ["ci", "ready", "deploy:qa", "deploy:staging", "deploy:production", "merged"]
    assert [n.label for n in m.nodes if n.kind == "deploy"] == ["qa", "staging", "production"]
    assert [n.order for n in m.nodes] == [0, 1, 2, 3, 4, 5]


def test_empty_deploy_envs_no_deploy_nodes():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=[])
    assert [n.id for n in m.nodes] == ["ci", "ready", "merged"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_build_stage_model.py -v`
Expected: FAIL (`build_stage_model() got an unexpected keyword argument 'deploy_envs'`)

- [ ] **Step 3: Write minimal implementation** — change the signature + deploy section:

```python
def build_stage_model(
    repo: str,
    *,
    ci_graph: dict | None,
    has_queue: bool,
    deploy_envs: list[str],
) -> StageModel:
    """Assemble the ordered StageModel for a repo from its real CI/CD facts."""
    specs: list[tuple[str, str, str]] = []  # (id, label, kind)
    ci_label = (ci_graph or {}).get("workflowName") or "CI"
    specs.append(("ci", ci_label, "ci"))
    specs.append(("ready", "Ready", "gate"))
    if has_queue:
        specs.append(("queue", "Queue", "queue"))
    for env in deploy_envs:
        specs.append((f"deploy:{env}", env, "deploy"))
    specs.append(("merged", "Merged", "merged"))
    nodes = tuple(
        StageNode(id=sid, label=label, kind=kind, order=i)
        for i, (sid, label, kind) in enumerate(specs)
    )
    return StageModel(repo=repo, nodes=nodes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_build_stage_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_build_stage_model.py
git commit -m "feat(pipeline): build_stage_model emits one deploy node per env"
```

---

### Task 3: `place_pr_on_track` multi-deploy placement

**Files:**
- Modify: `server/pipeline/track.py:90-121` (`place_pr_on_track`)
- Modify/extend: `server/tests/test_place_pr_on_track.py`

**Interfaces:**
- Consumes: `pr.deployed_envs` (added in Task 4 — but `getattr(pr, "deployed_envs", ())` keeps this task runnable before Task 4 lands; M-Track-1 `PullRequestInfo` has no field yet). Use `getattr` so the test PRs that set `deployed_envs` work and old ones default to `()`.

- [ ] **Step 1: Write the failing tests** (add to `server/tests/test_place_pr_on_track.py`)

```python
def _pr_envs(**kw):
    # helper: merged PR with deployed_envs; falls back to _pr() builder
    return _pr(**kw)


def test_merged_live_in_first_env_only():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "staging", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=("qa",))
    st = _states(pr, m)
    assert st == {"ci": "done", "ready": "done", "deploy:qa": "done",
                  "deploy:staging": "active", "deploy:production": "pending", "merged": "pending"}


def test_merged_live_in_all_envs_merged_done():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False,
                          deploy_envs=["qa", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=("qa", "production"))
    st = _states(pr, m)
    assert st == {"ci": "done", "ready": "done", "deploy:qa": "done",
                  "deploy:production": "done", "merged": "done"}


def test_merged_live_in_none_first_deploy_active():
    m = build_stage_model("o/r", ci_graph=None, has_queue=False, deploy_envs=["qa", "production"])
    pr = _pr(stage="merged", state="merged", merged=True, deployed_envs=())
    st = _states(pr, m)
    assert st["deploy:qa"] == "active"
    assert st["deploy:production"] == "pending"
    assert st["merged"] == "pending"
```

Keep all existing M-Track-1 tests (they use `deployed=True`/`deployed=False`); the back-compat bridge must keep them green. NOTE: Task 4 adds `deployed_envs` to `PullRequestInfo`; until then, `_pr(deployed_envs=...)` will fail — so **do Task 3's red/green using a tiny local stub** if needed, OR sequence Task 4 before Task 3. RECOMMENDED: implement Task 4 first (the field), then Task 3. (The plan lists them in dependency order for the reader; the implementer may do 4 then 3.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_place_pr_on_track.py -v`
Expected: FAIL (new multi-deploy assertions fail; merged PR currently uses single `deployed` bool cursor)

- [ ] **Step 3: Write minimal implementation** — replace the merged branch with per-node deploy logic:

```python
def place_pr_on_track(pr: "PullRequestInfo", model: StageModel) -> list["NodeStatus"]:
    ids = [n.id for n in model.nodes]
    order_by_id = {n.id: n.order for n in model.nodes}
    has_deploy_nodes = any(n.kind == "deploy" for n in model.nodes)

    if pr.stage == "merged":
        # Back-compat bridge: M-Track-1 tests set deployed=True without deployed_envs.
        deployed = set(getattr(pr, "deployed_envs", ()) or ())
        if pr.deployed:
            deployed.add("production")
        out: list[NodeStatus] = []
        frontier_used = False
        for n in model.nodes:
            if n.kind == "deploy":
                env = n.id.split("deploy:", 1)[1]
                if env in deployed:
                    state = "done"
                elif not frontier_used:
                    state = "active"
                    frontier_used = True
                else:
                    state = "pending"
            elif n.kind == "merged":
                if has_deploy_nodes:
                    state = "pending" if frontier_used else "done"
                else:
                    state = "active"  # M-Track-1: no deploy tracking → merged is the active terminal
            else:  # ci / ready / queue cleared by a merged PR
                state = "done"
            out.append(NodeStatus(id=n.id, state=state))
        return out

    # Non-merged PRs: single forward cursor by stage (unchanged from M-Track-1).
    target = _STAGE_TO_NODE.get(pr.stage, "ci")
    if target not in order_by_id:
        target = "ci" if "ci" in order_by_id else ids[0]
    cursor = order_by_id[target]
    out = []
    for n in model.nodes:
        if n.order < cursor:
            state = "done"
        elif n.order == cursor:
            failing = pr.checks == "failing" and n.kind in ("ci", "queue")
            state = "failed" if failing else "active"
        else:
            state = "pending"
        out.append(NodeStatus(id=n.id, state=state))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_place_pr_on_track.py -v`
Expected: PASS (all M-Track-1 cases + the 3 new multi-deploy cases)

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/track.py server/tests/test_place_pr_on_track.py
git commit -m "feat(pipeline): place_pr_on_track walks multiple deploy nodes via deployed_envs"
```

---

### Task 4: `PullRequestInfo.deployed_envs` + per-env deploy parsing

**Files:**
- Modify: `server/github/pr_status.py` — `PullRequestInfo` (field + `to_dict`/`from_dict`), `PRODUCTION_ENVIRONMENT` usage, `build_population_query`, `build_pr_query`, `_active_deploy_date` → `_active_deploy_dates`, `_is_deployed` → `_is_deployed_envs`, `_to_info`, and the two call sites passing `active_date` (`parse_population_response`, `parse_pr_response`).
- Test: `server/tests/test_deployed_envs.py`

**Interfaces:**
- Produces: `PullRequestInfo.deployed_envs: tuple[str, ...] = ()`; `deployed` bool = `"production" in deployed_envs`. `_active_deploy_dates(node) -> dict[str, str]`. `_is_deployed_envs(pr, state, active_dates) -> tuple[str, ...]`.

> **Do Task 4 before Task 3's red/green if convenient** (Task 3's tests construct `_pr(deployed_envs=…)`).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deployed_envs.py
from server.github.pr_status import (
    PullRequestInfo, _active_deploy_dates, _is_deployed_envs, build_population_query,
)


def test_active_deploy_dates_groups_by_env_newest_success():
    node = {"deployments": {"nodes": [
        {"latestEnvironment": "production", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-20T00:00:00Z"}},
        {"latestEnvironment": "production", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-10T00:00:00Z"}},  # older, ignored
        {"latestEnvironment": "staging", "latestStatus": {"state": "SUCCESS"},
         "commit": {"committedDate": "2026-06-25T00:00:00Z"}},
        {"latestEnvironment": "qa", "latestStatus": {"state": "FAILURE"},
         "commit": {"committedDate": "2026-06-26T00:00:00Z"}},  # not SUCCESS, ignored
    ]}}
    dates = _active_deploy_dates(node)
    assert dates == {"production": "2026-06-20T00:00:00Z", "staging": "2026-06-25T00:00:00Z"}


def test_is_deployed_envs_membership_by_merged_at():
    pr = {"mergedAt": "2026-06-15T00:00:00Z"}
    dates = {"production": "2026-06-20T00:00:00Z", "staging": "2026-06-10T00:00:00Z"}
    # live in production (merged 06-15 ≤ prod deploy 06-20); NOT staging (06-15 > 06-10)
    assert _is_deployed_envs(pr, "merged", dates) == ("production",)


def test_is_deployed_envs_empty_for_open():
    assert _is_deployed_envs({"mergedAt": None}, "open", {"production": "x"}) == ()


def test_pullrequestinfo_deployed_bool_tracks_production_env():
    pr = PullRequestInfo(number=1, url="u", state="merged", checks="passing", title="t",
                         deployed_envs=("qa", "production"))
    d = pr.to_dict()
    assert d["deployed_envs"] == ["qa", "production"]
    assert PullRequestInfo.from_dict(d).deployed_envs == ("qa", "production")


def test_population_query_unfiltered_deployments_with_latest_environment():
    q = build_population_query("o/r", "2026-06-20")
    assert "latestEnvironment" in q
    assert 'environments: ["production"]' not in q
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deployed_envs.py -v`
Expected: FAIL (`_active_deploy_dates` not defined; `deployed_envs` unknown kwarg)

- [ ] **Step 3: Write minimal implementation**

In `server/github/pr_status.py`:

(a) Add the field to `PullRequestInfo` (after `deployed`): `deployed_envs: tuple[str, ...] = ()`. In `to_dict` add `"deployed_envs": list(self.deployed_envs)`; in `from_dict` add `deployed_envs=tuple(d.get("deployed_envs", []))`. Redefine `deployed` derivation in `_to_info` (below) so the bool stays `"production" in deployed_envs`.

(b) The deployment query blocks — in BOTH `build_pr_query` (the per-pair `deployments(...)`) and `build_population_query` (the repo block) replace:
```
deployments(environments: ["production"], orderBy: {field: CREATED_AT, direction: DESC}, first: 5) {
  nodes { commitOid latestStatus { state } commit { committedDate } }
}
```
with:
```
deployments(orderBy: {field: CREATED_AT, direction: DESC}, first: 30) {
  nodes { latestEnvironment commitOid latestStatus { state } commit { committedDate } }
}
```
(Drop the `environments:` filter; add `latestEnvironment`; bump `first` to 30 to span multiple envs.)

(c) Replace `_active_deploy_date` with:
```python
def _active_deploy_dates(node: dict) -> dict[str, str]:
    """Map env name → committedDate of that env's newest SUCCESS deployment."""
    nodes = ((node.get("deployments") or {}).get("nodes")) or []
    out: dict[str, str] = {}
    for dep in nodes:  # newest-first
        env = dep.get("latestEnvironment")
        if not env or env in out:
            continue
        if (dep.get("latestStatus") or {}).get("state") != "SUCCESS":
            continue
        committed = (dep.get("commit") or {}).get("committedDate")
        if committed:
            out[env] = committed
    return out
```

(d) Replace `_is_deployed` with:
```python
def _is_deployed_envs(pr: dict, state: str, active_dates: dict[str, str]) -> tuple[str, ...]:
    merged_at = pr.get("mergedAt")
    if state != "merged" or not merged_at:
        return ()
    return tuple(env for env, date in active_dates.items() if date and merged_at <= date)
```

(e) Update `_to_info(pr, active_dates)` (rename the param from `active_date`): compute
`deployed_envs = _is_deployed_envs(pr, state, active_dates)` and set `deployed_envs=deployed_envs`,
`deployed=("production" in deployed_envs)` on the `PullRequestInfo`.

(f) Update the two callers to pass the dict: `parse_population_response` (`active_dates = _active_deploy_dates(repo_node)`) and `parse_pr_response` (`active_dates = _active_deploy_dates(node)`), passing `active_dates` into `_to_info`.

- [ ] **Step 4: Run test to verify it passes** (plus the existing pr_status tests, which must still pass)

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deployed_envs.py server/tests/test_pr_status.py server/tests/test_pr_population.py -v`
Expected: PASS. If an existing test asserts the old `deployments(environments: ["production"]...)` query string or the old `_active_deploy_date` name, update it to the new query/function (these are the intended changes — document which you updated).

- [ ] **Step 5: Commit**

```bash
git add server/github/pr_status.py server/tests/test_deployed_envs.py
git commit -m "feat(github): per-env deploy state (deployed_envs) from multi-env deployments query"
```

---

### Task 5: Orchestrator derives `deploy_envs` per repo

**Files:**
- Modify: `server/sessions/orchestrator.py` (`all_repos_stage_models`)
- Test: `server/tests/test_orchestrator_stage_models.py` (extend)

**Interfaces:**
- Consumes: `order_deploy_envs` (Task 1), `build_stage_model(deploy_envs=…)` (Task 2), `pr.deployed_envs` (Task 4).

- [ ] **Step 1: Write the failing test** (add to `server/tests/test_orchestrator_stage_models.py`)

```python
def test_all_repos_stage_models_builds_deploy_nodes_from_deployed_envs(monkeypatch):
    from server.github.pr_status import PullRequestInfo
    p1 = PullRequestInfo(number=1, url="u", state="merged", checks="passing", title="t",
                         repo="o/r", stage="merged", deployed_envs=("staging",))
    p2 = PullRequestInfo(number=2, url="u", state="merged", checks="passing", title="t",
                         repo="o/r", stage="merged", deployed_envs=("production", "qa"))
    orch = _mk_orch(monkeypatch, {"o/r": (p1, p2)})
    models = orch.all_repos_stage_models()
    ids = [n.id for n in models["o/r"].nodes]
    # union {staging, production, qa} ordered → qa, staging, production
    assert ids == ["ci", "ready", "deploy:qa", "deploy:staging", "deploy:production", "merged"]
```

(Reuse the existing `_mk_orch`/`_FakePop` helpers in that file. The existing
`test_all_repos_stage_models_adds_queue_and_deploy` uses `deployed=True` to get a deploy node — update
it: a PR with `deployed=True` now needs `deployed_envs=("production",)` to produce the node, since the
orchestrator builds `deploy_envs` from `deployed_envs`, not the bool. Set
`deployed_envs=("production",)` on that fixture PR and keep its assertion `deploy:production` present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_stage_models.py -v`
Expected: FAIL (orchestrator still passes `has_deploy=`; new test sees wrong node ids)

- [ ] **Step 3: Write minimal implementation** — in `all_repos_stage_models`, replace the `has_deploy` line and the `build_stage_model(...)` call:

```python
from server.pipeline.track import build_stage_model, order_deploy_envs
...
        prs = pop.get(repo, ())
        has_queue = any(p.stage == "queue" for p in prs)
        repo_envs = set()
        for p in prs:
            repo_envs.update(getattr(p, "deployed_envs", ()) or ())
        ci_graph = self._pr_population.history_store().ci_graph(repo) if ... else None  # keep existing ci_graph line
        models[repo] = build_stage_model(
            repo, ci_graph=ci_graph, has_queue=has_queue,
            deploy_envs=order_deploy_envs(repo_envs),
        )
```

(Keep the existing `ci_graph`/`history_store()` resolution exactly as it is; only the `has_deploy` →
`deploy_envs` part changes. Grep the current method body and edit surgically.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_stage_models.py server/tests/test_pipeline_route.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_stage_models.py
git commit -m "feat(pipeline): orchestrator builds deploy nodes from union of deployed_envs"
```

---

### Task 6: Frontend — confirm `StageTrack` renders N deploy nodes

**Files:**
- Modify: `web/src/components/__tests__/StageTrack.test.tsx`

**Interfaces:** none new — the dumb `StageTrack` already maps `model.nodes`. This task only locks the behavior with a test (and confirms env-name labels render).

- [ ] **Step 1: Write the failing/▸new test**

```tsx
it('renders one node per deploy env with env-name labels and states', () => {
  const model = { repo: 'o/r', nodes: [
    { id: 'ci', label: 'CI', kind: 'ci', order: 0, source: 'derived' },
    { id: 'ready', label: 'Ready', kind: 'gate', order: 1, source: 'derived' },
    { id: 'deploy:qa', label: 'qa', kind: 'deploy', order: 2, source: 'derived' },
    { id: 'deploy:staging', label: 'staging', kind: 'deploy', order: 3, source: 'derived' },
    { id: 'deploy:production', label: 'production', kind: 'deploy', order: 4, source: 'derived' },
    { id: 'merged', label: 'Merged', kind: 'merged', order: 5, source: 'derived' },
  ] } as const
  render(<StageTrack model={model} states={[
    { id: 'ci', state: 'done' }, { id: 'ready', state: 'done' },
    { id: 'deploy:qa', state: 'done' }, { id: 'deploy:staging', state: 'active' },
    { id: 'deploy:production', state: 'pending' }, { id: 'merged', state: 'pending' },
  ]} />)
  expect(screen.getByTestId('stage-node-deploy:qa')).toHaveAttribute('data-state', 'done')
  expect(screen.getByTestId('stage-node-deploy:staging')).toHaveAttribute('data-state', 'active')
  expect(screen.getByText('staging')).toBeInTheDocument()
  expect(screen.getByText('production')).toBeInTheDocument()
})
```

(Confirm the `data-testid` format matches `StageTrack.tsx` — it is `stage-node-${node.id}`, so `stage-node-deploy:qa`. If the colon causes a selector issue, assert via `getAllByTestId(/^stage-node-deploy:/)` or query by text label instead.)

- [ ] **Step 2: Run test to verify it fails/passes**

Run (in `web/`, after `npm ci`): `npx vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: PASS if StageTrack already renders generically (likely) — if so this is a confirming/regression test. If it FAILS (e.g. label not rendered, or deploy nodes mis-rendered), fix `StageTrack.tsx` minimally so it renders each node's `label` and `data-state`.

- [ ] **Step 3: Implementation** — only if Step 2 revealed a gap; otherwise none.

- [ ] **Step 4: Full frontend gate**

Run (in `web/`): `npx vitest run` then `npx tsc -b --force` then `npx eslint . --max-warnings 0`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/__tests__/StageTrack.test.tsx web/src/components/StageTrack.tsx
git commit -m "test(web): StageTrack renders N deploy nodes with env-name labels"
```

---

## Final verification (before PR)

- [ ] Backend full suite: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -rfE --tb=no -q` — capture FAILED/ERROR set, diff against an `origin/master` baseline worktree (memory: worktree-fullsuite-baseline-diff). Zero NEW non-environmental failures; re-run any candidate in isolation on both branches.
- [ ] Backend lint: `ruff check server` + `mypy server` (no new errors).
- [ ] Frontend: `vitest run` + `tsc -b --force` + `eslint --max-warnings 0` green.
- [ ] Grep `_active_deploy_date(` returns nothing (old single-env fn fully replaced); grep `has_deploy` in track.py/orchestrator returns nothing (param renamed).
- [ ] Grep `environments: ["production"]` and `PRODUCTION_ENVIRONMENT` — the filter is gone; `PRODUCTION_ENVIRONMENT` may remain only as the back-compat key for the `deployed` bool.

## Self-review (plan ↔ spec)

- Spec §3.1 query → Task 4(b). §3.2 `_active_deploy_dates` → Task 4(c). §3.3 `deployed_envs` → Task 4(a,d,e). §3.4 `order_deploy_envs` → Task 1. §3.5 `build_stage_model` → Task 2. §3.6 `place_pr_on_track` + back-compat bridge + no-deploy-merged-active → Task 3. §3.7 orchestrator → Task 5. §4 frontend test → Task 6. §7 testing folded per task + final gate.
- Dependency note: Task 4 (the `deployed_envs` field) is a prerequisite for Task 3's tests and Task 5; the implementer should land Task 4 before Task 3's red/green even though the plan numbers Task 3 first for narrative flow. Stated in both tasks.

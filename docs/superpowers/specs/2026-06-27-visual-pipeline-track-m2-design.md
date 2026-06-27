# M-Track-2: Multi-Environment Deploy Nodes — Design

**Date:** 2026-06-27
**Status:** Approved (design, flows from the program overview); pending implementation plan
**Program:** Visual Pipeline Track — see
`docs/superpowers/specs/2026-06-27-visual-pipeline-track-overview-design.md`. Second milestone, building
directly on M-Track-1 (`2026-06-27-visual-pipeline-track-m1-design.md`, merged in PR #333).

---

## 1. Goal

M-Track-1 renders a single hardcoded `deploy:production` node, gated on `has_deploy = any(pr.deployed)`.
M-Track-2 makes the deploy section **truthful to each repo's real environments**: enumerate the repo's
GitHub deployment environments and render one ordered `deploy:<env>` node per environment
(conventionally dev/qa → staging → production), with each node's state derived from whether the PR's
merge is live in that environment.

Because the frontend `StageTrack` already renders whatever nodes the backend emits (the M-Track-1
"dumb frontend" design), **this milestone is almost entirely backend**.

## 2. Non-goals

- **No Plotroom deploy-ancestry port** — we use GitHub's deployment + environment data directly, not a
  reconstructed promotion graph. "Live in env X" = the PR's merge commit is at/behind that env's newest
  successful deployment (the same date-comparison `_is_deployed` already uses), computed per env.
- **No per-env config surface** — environments come from GitHub (`Repository.environments`); no manual
  ordering/labels (that's the M-Track-4 override layer).
- **No new frontend feature** — only a test confirming the dumb track renders N deploy nodes + an env
  label. (M-Track-1 hardcoded the single deploy node's label as "Deploy"; now labels are env names.)
- **No new poller / no extra GitHub round-trip** — the env list + multi-env deployments are folded into
  the existing per-repo population query.

## 3. Architecture — Backend

### 3.1 Population query: enumerate environments + fetch multi-env deployments

`server/github/pr_status.py`, `build_population_query` (and the symmetric per-branch
`build_pr_query`'s deployment block):

- Replace the `deployments(environments: ["production"], …)` filter with an **un-filtered**, newest-first
  fetch tagging each deployment with its environment:
  ```
  deployments(orderBy: {field: CREATED_AT, direction: DESC}, first: 30) {
    nodes { latestEnvironment latestStatus { state } commit { committedDate } }
  }
  ```
  `Deployment.latestEnvironment` is **verified present** on the live GitHub GraphQL schema (probed
  2026-06-27 via `gh api graphql` against the `Deployment` type — it is the non-deprecated successor to
  `environment`).

**Decision (locked here, was deferred to the plan): Option A — derive the deploy node set from
per-PR `deployed_envs`, not a separate `environments` enumeration.** We do NOT add
`environments(first: 20) { nodes { name } }`. The repo's deploy nodes are the union of `deployed_envs`
across its population PRs (the envs that have actually received a successful deployment). This needs no
poller-cache change and no second data source. Trade-off (documented): an environment that exists in
GitHub but has **zero** successful deployments in the window won't render a node yet — acceptable for
M-Track-2; full environment enumeration is a later enhancement (and naturally belongs with the
M-Track-4 override layer).

### 3.2 Per-environment active-deploy dates

Replace `_active_deploy_date(node) -> str | None` with
`_active_deploy_dates(node) -> dict[str, str]` — env name → committedDate of that env's newest
**SUCCESS** deployment. Group the deployment nodes by `latestEnvironment`, keep the first (newest)
SUCCESS per env. `PRODUCTION_ENVIRONMENT` stays as the back-compat default key.

### 3.3 `PullRequestInfo` gains per-env deploy state

Add `deployed_envs: tuple[str, ...] = ()` — the environments where this PR is live (merged AND
`mergedAt <= active_dates[env]`). Keep the existing `deployed: bool` field, redefined as
`"production" in deployed_envs` (back-compat for any consumer reading the bool, incl. the `PrPill`
deployed rocket). Update `to_dict`/`from_dict` additively (default `()`).

- `_is_deployed_envs(pr, state, active_dates) -> tuple[str, ...]`: for a merged PR, every env whose
  active date is ≥ `mergedAt`. Empty for non-merged.
- `_to_info` sets both `deployed_envs` and the derived `deployed` bool.

### 3.4 Env ordering helper

New `server/pipeline/track.py:order_deploy_envs(envs: Iterable[str]) -> list[str]` — deterministic
conventional ordering by keyword rank, tie-broken alphabetically:

| Rank | Keyword (substring, lowercased)                  |
|------|--------------------------------------------------|
| 0    | `dev`, `development`                              |
| 1    | `test`, `qa`, `integration`                      |
| 2    | (unknown / other)                                |
| 2    | `stage`, `staging`, `preprod`, `uat`, `sandbox`  |
| 3    | `prod`, `production`, `live`, `release`          |

Sort key = `(rank, name)`. Production-family always last; dev/qa first; unknown envs cluster in the
middle, alphabetical. (Pure + unit-tested; no I/O.)

### 3.5 `build_stage_model`: `has_deploy: bool` → `deploy_envs: list[str]`

`server/pipeline/track.py`. Replace the `has_deploy` parameter with `deploy_envs: list[str]` (already
ordered by the caller). Emit one node per env: `id = f"deploy:{env}"`, `label = env`, `kind = "deploy"`,
in list order, between `queue` and `merged`. Empty `deploy_envs` → no deploy nodes (collapses to the
M-Track-1 no-deploy shape). The single-prod case (`["production"]`) yields `deploy:production` labeled
`"production"` — note the label changes from M-Track-1's `"Deploy"` to the env name (intended:
truthful).

### 3.6 `place_pr_on_track`: multi-deploy placement

`server/pipeline/track.py`. Generalize from the single-deploy cursor to N deploy nodes, driven by
`pr.deployed_envs`:

- Non-merged PRs: unchanged (cursor at ci/ready/queue per stage; all deploy + merged nodes `pending`).
- Merged PRs:
  - all pre-deploy nodes (ci, ready, queue) → `done`;
  - each `deploy:<env>` node → `done` iff `env ∈ pr.deployed_envs`;
  - the **frontier** = the first deploy node (in model order) whose env ∉ `deployed_envs` → `active`;
    deploy nodes after the frontier → `pending`;
  - `merged` terminal node → `done` iff there is no frontier (live in every env), else `pending`.
- A `deployed_envs` that skips an env (e.g. live in prod but not staging) renders that skipped env
  `done`/`pending` strictly by membership, with the frontier at the first gap — a documented edge; the
  common promotion case (a contiguous prefix) renders cleanly.

`place_pr_on_track` reads `pr.deployed_envs`; it still derives nothing about *stages* (constitution VI).

**Back-compat bridge (keeps M-Track-1 tests green):** build the working set as
`deployed = set(pr.deployed_envs or ()); if pr.deployed: deployed.add("production")`. M-Track-1's
placement tests construct `PullRequestInfo(deployed=True)` without `deployed_envs`; the bridge re-adds
`"production"` so the single `deploy:production` node still resolves to `done`. In M-Track-2 the
`deployed` bool is itself `"production" ∈ deployed_envs`, so the bridge is a no-op on real data.

**No-deploy-node merged case is unchanged from M-Track-1:** when the model has **no** deploy nodes, a
merged PR's `merged` node stays `active` (not `done`) — preserve that exact behavior; only the
with-deploy-nodes merged case uses the frontier rule (merged `done` iff live in every env).

### 3.7 Orchestrator wiring

`server/sessions/orchestrator.py:all_repos_stage_models()`: replace the `has_deploy = any(p.deployed)`
heuristic with `repo_envs = set().union(*(p.deployed_envs for p in prs))` and pass
`deploy_envs = order_deploy_envs(repo_envs)` into `build_stage_model` (Option A — see §3.1). No poller
cache change. A repo whose PRs have no `deployed_envs` yields `deploy_envs=[]` → the M-Track-1 no-deploy
shape (`CI → Ready → Merged`).

## 4. Architecture — Frontend

No feature change. The dumb `StageTrack` already maps `model.nodes` → circles and renders each node's
`label`. Two small confirmations:
- A `StageTrack` test asserting N deploy nodes (`deploy:qa`, `deploy:staging`, `deploy:production`)
  render with their env-name labels and correct per-node states.
- Confirm the M-Track-1 skeleton/fallback still behaves (no deploy nodes in the skeleton — unchanged).

## 5. Data flow

```
per-repo population query
  └─ deployments(first:30){latestEnvironment,…}    → _active_deploy_dates(node): env → date
        │
PullRequestInfo.deployed_envs = envs where mergedAt ≤ active_date[env]   (deployed bool = 'production' ∈ that)
        │
orchestrator.all_repos_stage_models():
   repo_envs = ∪ p.deployed_envs ;  deploy_envs = order_deploy_envs(repo_envs) → build_stage_model(repo, …, deploy_envs)
        │
all_prs(): place_pr_on_track(pr, model) walks deploy nodes via pr.deployed_envs
        │
GET /api/pipeline/repos (models) + GET /api/prs (node_states) → StageTrack renders N deploy nodes
```

## 6. Error handling & edge cases

- **Repo with no environments** → `deploy_envs=[]` → no deploy nodes (M-Track-1 no-deploy shape). Never blank.
- **Env with no successful deployment** → not in any `deployed_envs`; node renders (if enumerated) as
  `pending`/`active` frontier for merged PRs. Truthful ("not live there yet").
- **`latestEnvironment` null on a deployment node** → skipped in grouping (defensive).
- **More than 20 envs / 30 recent deployments** → bounded fetch; rare for real repos. Document the cap;
  do not silently imply full coverage (working norm: no silent caps).
- **Back-compat:** `deployed` bool unchanged in meaning for production; `deployed_envs` defaults `()`;
  serialization additive (no migration).

## 7. Testing (TDD)

**Backend**
- `order_deploy_envs`: dev/qa→staging→prod ordering; unknown env placement; alphabetical tie-break; `[]`.
- Query builder: `build_population_query` uses the un-filtered `deployments` with `latestEnvironment`
  (no `environments:["production"]` filter); per-branch `build_pr_query` deployment block updated symmetrically.
- `_active_deploy_dates`: groups by env, newest SUCCESS per env, ignores non-SUCCESS / null env.
- `_is_deployed_envs` / `_to_info`: per-env membership by `mergedAt ≤ active_date`; `deployed` bool =
  `'production' ∈ deployed_envs`; non-merged → `()`.
- `PullRequestInfo` `to_dict`/`from_dict` round-trip incl. `deployed_envs`.
- `build_stage_model(deploy_envs=[...])`: one ordered `deploy:<env>` node per env, env-name labels,
  between queue and merged; `[]` → none.
- `place_pr_on_track` multi-deploy: merged + live-in-[qa] → qa done, staging active, prod+merged
  pending; live-in-all → all deploy done + merged done; merged + live-in-none → first deploy active;
  the skipped-env edge.
- `all_repos_stage_models`: builds deploy nodes from the repo env set (or union fallback); real
  `GET /api/pipeline/repos` shows multiple deploy nodes; `all_prs()` attaches correct multi-deploy
  `node_states` on a **real** GET.
- Full backend suite: no new failures vs `origin/master` (use the baseline-diff technique —
  memory: worktree-fullsuite-baseline-diff).

**Frontend (vitest)**
- `StageTrack` renders N deploy nodes with env-name labels + per-node states.
- Full `vitest run` + `tsc -b --force` + `eslint --max-warnings 0` green.

## 8. Files touched (summary)

**Modified**
- `server/github/pr_status.py` — query (`environments` + multi-env `deployments`), `_active_deploy_dates`,
  `_is_deployed_envs`, `PullRequestInfo.deployed_envs` + serializers, `_to_info`.
- `server/pipeline/track.py` — `order_deploy_envs`; `build_stage_model` `deploy_envs` param;
  `place_pr_on_track` multi-deploy placement.
- `server/sessions/orchestrator.py` — `repo_envs = ∪ deployed_envs` → `order_deploy_envs` → `build_stage_model`.
- `web/src/components/__tests__/StageTrack.test.tsx` — multi-deploy-node render test.

(No `server/github/pr_population.py` change — Option A needs no poller-cache addition.)

## 9. Risks

- **GraphQL field drift** (`latestEnvironment`): mitigated by the §3.1 verify-against-live-schema step.
- **Deployment fetch cap** (first:30 across all envs): could miss an env's active deploy on a very
  busy repo; `log`/document the cap, don't imply full coverage.
- **Dual serializer / exact-shape tests:** `deployed_envs` added to `PullRequestInfo.to_dict` — assert
  on real responses; update any exact-dict PR test (memory: pullrequestinfo-todict-exact-shape-test,
  session-record-dual-serializer).
- **Env-set threading:** if the poller can't cheaply expose the env list, the union-of-`deployed_envs`
  fallback is acceptable and documented — never add a config surface to compensate.

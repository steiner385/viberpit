# PR "Deployed" Indicator — Design

**Date:** 2026-06-23
**Status:** Approved (design); implementation pending
**Feature area:** PR status badges (`PrPill`) + GitHub deployment publishing

## Goal

Each PR indicator badge should show whether that PR's code is **live in production**.
GitHub Deployments are the cross-repo source of truth: any repo that publishes
`production` deployment records lights up its badges automatically. ccswitch is the
first repo to publish its own records, and its deployer serves as the reference
implementation other repos copy.

## Why GitHub Deployments (architecture decision)

`gh` exposes no deploy signal for this project today because nothing creates
deployment records — ccswitch deploys via a self-hosted poll-and-pull loop
(`scripts/auto-deploy.sh`), and CI green ≠ deployed. Rather than teach the consumer
to reach into each repo's bespoke deploy state, we make **GitHub the source of truth**
and put the burden of keeping it current on each repo (the producer). The consumer
(ccswitch badges) then reads one standard, already-batched signal.

```
Repo's deployer  ──POST deployment + success status──▶  GitHub Deployments (source of truth)
(ccswitch: auto-deploy.sh)                                      │
                                                                ▼
ccswitch backend ──batched gh api graphql (PRs + active deploy per repo)──▶ classify ──▶ badge
```

## Semantics: "live in prod"

GitHub Deployment records are bound to a **commit**, but the badge is per **PR**, and
ccswitch's deployer ships `origin/master` HEAD — so if two PRs merge between deploy
ticks, the intermediate commit is never shipped on its own. A naive "does this PR's
merge commit have a deployment?" mapping would leave such PRs dark even though their
code is live.

Instead, "deployed" means: **the PR is merged AND its code is contained in the
currently-active production deployment.** We determine containment with a **timestamp
comparison** (chosen over a true git-ancestry check):

```
deployed = (state == "merged")
           and pr.mergedAt is present
           and active_deploy_committed_date is present
           and pr.mergedAt <= active_deploy_committed_date
```

- Uses only fields already in the GraphQL response — zero extra API calls, scales to
  any number of repos for free.
- Accepted approximation cost: a reverted/rolled-back PR would still read "deployed,"
  and two merges in the same second could tie. Both acceptable for a status badge.
  Additionally, a rebased HEAD commit whose `committedDate` predates its own PR's `mergedAt`
  could leave that one PR's badge briefly dark until the next deploy — self-healing and cosmetic.
- Rejected alternative — REST `compare/{mergeSha}...{activeSha}` per merged PR: exact,
  but one REST call per merged PR per poll against the shared 5k/hr limit, contrary to
  the project's "batched GraphQL for reads" norm.

## Component 1 — Consumer: read deployments

**Files:** `server/github/pr_status.py` (pure helpers), `server/github/pr_poller.py`
(unchanged behavior; benefits automatically since the helpers do the work).

Extend each per-repo `repository(...)` alias to also fetch the active production
deployment in the **same** block — still one batched call, one alias per repo:

```graphql
rN: repository(owner: "...", name: "...") {
  pullRequests(headRefName: "...", first: 5, orderBy: {field: UPDATED_AT, direction: DESC}) {
    nodes { number url title state isDraft updatedAt mergedAt
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }
  }
  deployments(environments: ["production"], orderBy: {field: CREATED_AT, direction: DESC}, first: 5) {
    nodes { commitOid latestStatus { state } commit { committedDate } }
  }
}
```

Changes vs. today: new PR field `mergedAt`; new `deployments` block.

**Classification (per repo):**
- Active prod commit = the newest deployment node whose `latestStatus.state == "SUCCESS"`;
  read its `commit.committedDate`.
- Add `deployed: bool` to `PullRequestInfo`, computed with the timestamp rule above
  using that repo's active commit date.

**Graceful absence:** a repo with no `production` deployments yields no active commit →
every PR's `deployed` is `False`. No errors, no special-casing. This is the default for
all repos that haven't onboarded.

**Single source of truth:** define `PRODUCTION_ENVIRONMENT = "production"` once and
reference it from the query builder (constitution VI).

## Component 2 — Consumer: surface the field

- `PullRequestInfo.to_dict()` includes `"deployed"`.
- `web/src/api/types.ts` `PullRequestInfo` gains `deployed: boolean`.

## Component 3 — Consumer: badge UI

**File:** `web/src/components/PrPill.tsx`.

When `pr.deployed` is true (only ever true for merged PRs), render a compact "live"
affordance alongside the existing pill — a small green ● + `Deployed` label (or a 🚀
glyph), with `title`/`aria-label` "Deployed to production". Subtle, reusing the pill's
existing color/spacing tokens. No change to non-deployed PRs.

## Component 4 — Producer: ccswitch publishes its own

**File:** `scripts/auto-deploy.sh`.

After the existing `/healthz` SHA-match gate passes, create the record via the `gh api`
path already used in the script:

1. `POST /repos/{owner}/{repo}/deployments` with `ref=<shipped sha>`,
   `environment=production`, `production_environment=true`, `required_contexts=[]`,
   `auto_merge=false`.
   - `required_contexts=[]` is essential — otherwise GitHub refuses to create the
     deployment when statuses are pending/failing and returns a non-deployment payload.
2. `POST /repos/.../deployments/{id}/statuses` with `state=success`, `auto_inactive=true`
   — auto-marks the prior production deployment inactive, so exactly one is current.

**Non-fatal:** the deploy has already succeeded by this point; the badge is cosmetic.
Any failure to publish (missing token/permission, API error) logs a warning and never
fails the deploy or triggers rollback. Wrap so it cannot break production deploys.

## Testing

- **Pure helpers (network-free):**
  - `build_pr_query` includes the `deployments` block and `mergedAt`.
  - `parse_pr_response` / classification:
    - merged before active deploy commit ⇒ `deployed = True`
    - merged after active deploy commit ⇒ `deployed = False`
    - no deployments block / empty ⇒ `deployed = False`
    - non-merged PR ⇒ `deployed = False`
    - deployment whose `latestStatus.state != SUCCESS` is ignored when picking active
- **Frontend:** `PrPill` renders the deployed affordance only when
  `deployed && state === 'merged'`.
- **auto-deploy.sh:** manual verification on the live host (the script has no unit
  tests today); guard the `gh` calls so they are observable in the deploy log.

## Decisions made without further input

- Environment name hardcoded to `"production"` (single constant).
- Publish failures are non-fatal warnings.

## Out of scope (v1)

- "Deploying" / "failed" sub-states (reintroduce per-commit coupling; deploy window is ~30s).
- True git-ancestry reachability.
- A separate reusable cross-repo doc/snippet (auto-deploy.sh is the reference).
- Per-repo configurable environment name.

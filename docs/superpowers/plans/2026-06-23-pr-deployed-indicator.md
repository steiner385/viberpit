# PR "Deployed" Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show on each PR badge whether that PR's code is live in production, using GitHub Deployments as the cross-repo source of truth.

**Architecture:** Producer/consumer split. The consumer (ccswitch backend `pr_status.py` helpers) extends its existing batched `gh api graphql` call to also read each repo's active `production` deployment, and classifies a merged PR as `deployed` when it merged at-or-before that deployment's commit. The producer (`scripts/auto-deploy.sh`) publishes a `production` Deployment + success status to GitHub after its post-restart health gate passes. The React `PrPill` renders a "deployed" affordance.

**Tech Stack:** Python (pure helpers, pytest) · React + TypeScript + Tailwind + lucide-react (vitest) · bash + `gh` CLI.

## Global Constraints

- **Deploy semantics:** `deployed = (state == "merged") and mergedAt and active_deploy_committed_date and mergedAt <= active_deploy_committed_date`. ISO-8601 UTC strings compare lexicographically — string `<=` is correct.
- **Environment name:** the single constant `PRODUCTION_ENVIRONMENT = "production"`; never inline the literal elsewhere (constitution VI).
- **Graceful absence:** a repo with no `production` deployments ⇒ every PR `deployed = False`. No errors.
- **Producer is non-fatal:** publishing must never fail the deploy or trigger rollback — warn and continue.
- **Backend test env (worktree):** the venv's editable `ccswitch` resolves to the stale deploy checkout, so run backend tests with `PYTHONPATH=src` against the prebuilt env, e.g. `PYTHONPATH=src python -m pytest …` (see memory `ccswitch-worktree-backend-pythonpath`). `pr_status` tests are pure — no DB/auth/allowlist needed.
- **Frontend test env:** run in `web/` with the local binary (`./node_modules/.bin/vitest`), `node_modules` symlinked from the deploy worktree (memory `ccswitch-web-node-modules-source`). Real type gate is `tsc -b`, not `tsc --noEmit` (memory `ccswitch-frontend-real-typecheck`).

---

### Task 1: Backend — read deployments and classify `deployed`

**Files:**
- Modify: `server/github/pr_status.py`
- Test: `server/tests/test_pr_status.py`

**Interfaces:**
- Consumes: nothing new (existing `gitrepos` pairs feed `build_pr_query`).
- Produces:
  - `PRODUCTION_ENVIRONMENT: str = "production"`
  - `PullRequestInfo` gains a final field `deployed: bool = False`; `to_dict()` includes `"deployed"`.
  - `build_pr_query(pairs)` emits, inside each `rN: repository(...)` block, a `deployments(environments: ["production"], ...)` sub-block and a `mergedAt` field on PR nodes.
  - `parse_pr_response(response, pairs)` returns `PullRequestInfo`s with `deployed` populated.

- [ ] **Step 1: Write failing tests for the query shape and classification**

Append to `server/tests/test_pr_status.py`:

```python
from server.github.pr_status import PRODUCTION_ENVIRONMENT


def _resp_full(alias: str, pr_nodes: list, deploy_nodes: list) -> dict:
    return {"data": {alias: {
        "pullRequests": {"nodes": pr_nodes},
        "deployments": {"nodes": deploy_nodes},
    }}}


def _merged_pr(number, *, merged_at):
    pr = _pr(number, "MERGED")
    pr["mergedAt"] = merged_at
    return pr


def _deploy(commit_date, *, status="SUCCESS"):
    return {
        "commitOid": "deadbeef",
        "latestStatus": {"state": status} if status else None,
        "commit": {"committedDate": commit_date},
    }


def test_build_query_includes_deployments_and_merged_at():
    q = build_pr_query([("o/r", "br")])
    assert "deployments(environments:" in q
    assert PRODUCTION_ENVIRONMENT in q
    assert "mergedAt" in q


def test_deployed_true_when_merged_before_active_deploy():
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")],
        [_deploy("2026-06-22T12:00:00Z")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is True


def test_not_deployed_when_merged_after_active_deploy():
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T13:00:00Z")],
        [_deploy("2026-06-22T12:00:00Z")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_not_deployed_when_no_deployments():
    pairs = [("o/r", "br")]
    resp = _resp_full("r0", [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")], [])
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_not_deployed_when_pr_open():
    pairs = [("o/r", "br")]
    resp = _resp_full("r0", [_pr(5, "OPEN")], [_deploy("2026-06-22T12:00:00Z")])
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is False


def test_active_deploy_ignores_non_success_status():
    # Newest deploy is still in-progress; the older SUCCESS one is the active commit.
    pairs = [("o/r", "br")]
    resp = _resp_full(
        "r0",
        [_merged_pr(5, merged_at="2026-06-22T10:00:00Z")],
        [_deploy("2026-06-22T14:00:00Z", status="IN_PROGRESS"),
         _deploy("2026-06-22T11:00:00Z", status="SUCCESS")],
    )
    assert parse_pr_response(resp, pairs)[("o/r", "br")].deployed is True


def test_deployed_defaults_false_in_dict():
    info = PullRequestInfo(1, "u", "open", "none", "t")
    assert info.to_dict()["deployed"] is False
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH=src python -m pytest server/tests/test_pr_status.py -v`
Expected: the new tests FAIL (ImportError on `PRODUCTION_ENVIRONMENT` / `deployed` attribute / missing `mergedAt` in query). Pre-existing tests still PASS.

- [ ] **Step 3: Add the constant and the `deployed` field**

In `server/github/pr_status.py`, after the imports add:

```python
PRODUCTION_ENVIRONMENT = "production"
```

Extend the dataclass (append the new field last so existing positional construction keeps working) and `to_dict`:

```python
@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    state: str   # 'open' | 'draft' | 'merged' | 'closed'
    checks: str  # 'passing' | 'failing' | 'pending' | 'none'
    title: str
    deployed: bool = False  # merged AND live in the active production deployment

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "checks": self.checks,
            "title": self.title,
            "deployed": self.deployed,
        }
```

- [ ] **Step 4: Extend the GraphQL query**

In `build_pr_query`, replace the per-pair `blocks.append(...)` body so each repo block requests `mergedAt` on PR nodes and a `deployments` sub-block:

```python
        blocks.append(
            f"{_alias(i)}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
            f"  pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
            f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{\n"
            f"    nodes {{ number url title state isDraft updatedAt mergedAt\n"
            f"      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}\n"
            f"    }}\n"
            f"  }}\n"
            f"  deployments(environments: [{json.dumps(PRODUCTION_ENVIRONMENT)}], "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}, first: 5) {{\n"
            f"    nodes {{ commitOid latestStatus {{ state }} commit {{ committedDate }} }}\n"
            f"  }}\n"
            f"}}"
        )
```

- [ ] **Step 5: Add deployment parsing + classification**

In `server/github/pr_status.py` add a helper and thread the active date through `_to_info` / `parse_pr_response`:

```python
def _active_deploy_date(node: dict) -> str | None:
    """committedDate of the newest production deployment whose latest status is SUCCESS."""
    nodes = ((node.get("deployments") or {}).get("nodes")) or []
    for dep in nodes:  # already ordered newest-first by the query
        status = (dep.get("latestStatus") or {}).get("state")
        if status == "SUCCESS":
            return (dep.get("commit") or {}).get("committedDate")
    return None


def _is_deployed(pr: dict, state: str, active_date: str | None) -> bool:
    merged_at = pr.get("mergedAt")
    return bool(state == "merged" and merged_at and active_date and merged_at <= active_date)
```

Change `_to_info` to accept the active date:

```python
def _to_info(pr: dict, active_date: str | None) -> PullRequestInfo:
    state = _classify_state(pr.get("state", ""), bool(pr.get("isDraft")))
    return PullRequestInfo(
        number=pr["number"],
        url=pr["url"],
        state=state,
        checks=_classify_checks(_rollup_state(pr)),
        title=pr.get("title", ""),
        deployed=_is_deployed(pr, state, active_date),
    )
```

In `parse_pr_response`, compute the active date once per alias and pass it in. Replace the loop body so it reads:

```python
    for i, key in enumerate(pairs):
        node = data.get(_alias(i))
        if not node:
            continue
        active_date = _active_deploy_date(node)
        prs = ((node.get("pullRequests") or {}).get("nodes")) or []
        best: PullRequestInfo | None = None
        best_sort: tuple | None = None
        for raw in prs:
            info = _to_info(raw, active_date)
            sort_key = (_STATE_RANK.get(info.state, 99), _neg_iso(raw.get("updatedAt", "")))
            if best_sort is None or sort_key < best_sort:
                best, best_sort = info, sort_key
        if best is not None:
            out[key] = best
```

- [ ] **Step 6: Run the full file to verify all tests pass**

Run: `PYTHONPATH=src python -m pytest server/tests/test_pr_status.py -v`
Expected: PASS (new + pre-existing).

- [ ] **Step 7: Verify the API passthrough still works**

The HTTP layer serializes via `to_dict()`, so the new field flows automatically. Confirm nothing asserts the exact old dict shape:

Run: `PYTHONPATH=src python -m pytest server/tests/test_pr_status.py server/tests/test_pr_poller.py server/tests/test_sessions_pr_field.py -v`
Expected: PASS. If `test_sessions_pr_field.py` asserts an exact dict equal to the old shape, update that assertion to include `"deployed": False`.

- [ ] **Step 8: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status.py
git commit -m "feat(pr-badges): classify a merged PR as deployed via active production deployment"
```

---

### Task 2: Frontend — type + `PrPill` deployed affordance

**Files:**
- Modify: `web/src/api/types.ts:106-112`
- Modify: `web/src/components/PrPill.tsx`
- Test: `web/src/components/__tests__/PrPill.test.tsx`

**Interfaces:**
- Consumes: backend `PullRequestInfo` JSON now carries `deployed: boolean`.
- Produces: `PrPill` renders a `data-deployed="true"` marker + a rocket glyph when `pr.deployed`.

- [ ] **Step 1: Write failing tests**

Append inside the `describe('PrPill', …)` block in `web/src/components/__tests__/PrPill.test.tsx`:

```tsx
  it('shows a deployed affordance for a deployed merged PR', () => {
    render(<PrPill pr={{ ...base, state: 'merged', deployed: true }} />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('data-deployed', 'true')
    expect(link.getAttribute('aria-label')).toMatch(/deployed/i)
  })

  it('shows no deployed affordance when not deployed', () => {
    render(<PrPill pr={{ ...base, state: 'merged', deployed: false }} />)
    expect(screen.getByRole('link')).not.toHaveAttribute('data-deployed', 'true')
  })
```

Also add `deployed: false` to the shared `base` object so it satisfies the type:

```tsx
const base: PullRequestInfo = {
  number: 42, url: 'https://github.com/o/r/pull/42',
  state: 'open', checks: 'passing', title: 'Add thing', deployed: false,
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/PrPill.test.tsx`
Expected: FAIL (no `data-deployed`; and a type error on `base` until the type is updated).

- [ ] **Step 3: Add the type field**

In `web/src/api/types.ts` extend the interface:

```ts
export interface PullRequestInfo {
  number: number
  url: string
  state: 'open' | 'draft' | 'merged' | 'closed'
  checks: 'passing' | 'failing' | 'pending' | 'none'
  title: string
  deployed: boolean
}
```

- [ ] **Step 4: Render the affordance in `PrPill`**

In `web/src/components/PrPill.tsx`, add `Rocket` to the lucide import:

```tsx
import { GitPullRequest, GitMerge, GitPullRequestClosed, Check, X, Dot, Rocket } from 'lucide-react'
```

Then in the component, compute the label and render the marker. Replace the `checksLabel`/return region:

```tsx
export function PrPill({ pr }: { pr: PullRequestInfo }) {
  const StateIcon = STATE_ICON[pr.state]
  const checksLabel = pr.checks === 'none' ? '' : `, checks ${pr.checks}`
  const deployedLabel = pr.deployed ? ', deployed' : ''
  return (
    <a
      href={pr.url}
      target="_blank"
      rel="noopener noreferrer"
      data-state={pr.state}
      data-checks={pr.checks}
      data-deployed={pr.deployed ? 'true' : undefined}
      onClick={(e) => e.stopPropagation()}
      title={`${pr.title} — ${pr.state}${checksLabel}${deployedLabel}`}
      aria-label={`Pull request #${pr.number}, ${pr.state}${checksLabel}${deployedLabel}`}
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        STATE_STYLES[pr.state],
      )}
    >
      <StateIcon size={11} aria-hidden />
      <span className="tabular-nums">#{pr.number}</span>
      <ChecksIcon checks={pr.checks} />
      {pr.deployed && <Rocket size={11} aria-hidden className="text-emerald-600 dark:text-emerald-400" />}
    </a>
  )
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/PrPill.test.tsx`
Expected: PASS.

- [ ] **Step 6: Typecheck the whole web build (the real gate)**

Run (in `web/`): `npx tsc -b --force`
Expected: no errors. This catches any other fixture/consumer constructing `PullRequestInfo` without `deployed` — notably `web/src/components/__tests__/SessionCardPr.test.tsx`. If it flags one, add `deployed: false` to that fixture object and re-run.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/components/PrPill.tsx web/src/components/__tests__/PrPill.test.tsx
git commit -m "feat(pr-badges): render a deployed affordance on the PR pill"
```

---

### Task 3: Producer — publish a production Deployment from `auto-deploy.sh`

**Files:**
- Modify: `scripts/auto-deploy.sh` (insert after the "live + healthy" log at line 261)

**Interfaces:**
- Consumes: `$REPO`, `$REMOTE` (full shipped SHA), `gh` authenticated (already used for `check-runs`).
- Produces: a GitHub `production` Deployment + `success` status for the shipped commit, so the consumer query in Task 1 sees an active deployment.

- [ ] **Step 1: Add the non-fatal publish block**

In `scripts/auto-deploy.sh`, immediately after line 261 (`echo "[deploy] live + healthy at ${REMOTE:0:9}"`), insert:

```bash
# Publish a GitHub Deployment so PR badges (and any other consumer) can see that
# this commit is live in production. Best-effort: the deploy already succeeded, so
# a publish failure only warns — it must never fail the deploy or trigger rollback.
# required_contexts=[] is required, else GitHub refuses to create the deployment
# while CI statuses are pending and returns a non-deployment payload instead.
# JSON bodies are piped via --input - so required_contexts can be a true empty
# array — there is no reliable `gh -f` flag form for an empty array, and omitting
# the key makes GitHub apply the repo's default required contexts (causing a
# non-deployment "conflict" payload while CI is pending).
publish_deployment() {
  local sha="$1" dep_id
  dep_id="$(printf '{"ref":"%s","environment":"production","production_environment":true,"auto_merge":false,"required_contexts":[]}' "$sha" \
    | gh api -X POST "repos/{owner}/{repo}/deployments" --input - --jq '.id' 2>/dev/null)" || return 1
  [ -n "$dep_id" ] || return 1
  printf '{"state":"success","auto_inactive":true}' \
    | gh api -X POST "repos/{owner}/{repo}/deployments/${dep_id}/statuses" --input - --jq '.id' >/dev/null 2>&1 || return 1
}

if publish_deployment "$REMOTE"; then
  echo "[deploy] published production deployment for ${REMOTE:0:9}"
else
  echo "[deploy] WARNING: could not publish GitHub deployment for ${REMOTE:0:9} (token/permission/network?)" >&2
fi
```

- [ ] **Step 2: Lint the script**

Run: `bash -n scripts/auto-deploy.sh`
Expected: no syntax errors (exit 0). If `shellcheck` is available: `shellcheck scripts/auto-deploy.sh` — expect no new errors from the added block.

- [ ] **Step 3: Verify the gh calls are well-formed (no live deploy needed)**

Confirm the request shape against `gh`'s help so the flags are valid without mutating anything:

Run: `gh api --help | grep -E '\-\-(input|jq)'`
Expected: `--input` and `--jq` both listed — confirming the JSON-stdin + jq form used above is supported by the installed `gh`.

- [ ] **Step 4: Commit**

```bash
git add scripts/auto-deploy.sh
git commit -m "feat(deploy): publish a GitHub production deployment after the health gate"
```

---

## Manual verification (after all tasks land, on merge to production)

The producer has no unit test (the script isn't unit-tested today). After this ships and `auto-deploy.sh` runs once on the live host:

1. Check the deploy log for `published production deployment for <sha>`.
2. `gh api repos/{owner}/{repo}/deployments --jq '.[0] | {sha: .sha, env: .environment}'` shows the latest commit in `production`.
3. In the web UI, a recently-merged PR's pill shows the rocket affordance within one poll interval (~75s).
4. A merged PR from *before* the deployment's commit also shows deployed (timestamp rule); an open PR never does.

# PR Consolidation for PR Doctor — Design

**Date:** 2026-07-04
**Status:** Approved design, pending implementation plan
**Scope:** Let PR Doctor consolidate several selected open PRs into a single PR — spawning one
account-rotated `claude` session to merge them and close the originals — and *recommend* which PRs
to consolidate based on similarity / low risk.

## Problem

Every open PR runs its own CI. When several small, related PRs sit open at once (e.g. a cluster of
`fix(deploy):` changes, or a batch of independent low-risk edits), that's N parallel CI runs and N
merge-queue passes for what could be one. There is no way today to say "these three belong
together — combine them into one PR" and no help identifying which PRs are cheap to combine.

PR Doctor already watches CI and *restores* failing PRs by spawning sessions; it has no notion of
*combining* healthy ones. This design adds that.

## Goal

A PR-doctor surface where the user can:

1. **Select** ≥2 open PRs in one repo — by hand (checkbox multi-select) **or** by one-tapping a
   **recommended cluster** — and
2. **Consolidate** them: spawn a single account-rotated `claude` session that branches off
   `origin/master`, merges/cherry-picks each selected PR, resolves conflicts, writes a combined
   body with `Closes #N` per original, pushes, opens the consolidated PR, then **closes each
   original with a "Superseded by #X" comment**.

Plus a recommender that clusters open PRs by similarity/low-risk and surfaces suggested sets.

**Non-goals (YAGNI, explicitly deferred):**
- **No auto mode.** Consolidation closes PRs; it *only ever* runs on an explicit human tap. The
  recommender suggests, it never acts.
- **No cross-repo consolidation** — you can't merge branches across repos; candidates group by repo.
- **No file/author/label-based recommendation in v1** — those signals aren't fetched yet (see
  "GraphQL / metadata" below). v1 clusters on already-available signals; enrichment is v1.5.
- No consolidation-history/audit log, no undo (the originals' branches still exist on GitHub if a
  human needs to reopen).

## Architecture — reuse, don't reinvent

The feature maps onto existing grooves; the only net-new *logic* is the recommender heuristic and
the consolidation prompt. Everything else is wiring.

| Layer | File | Change |
|-------|------|--------|
| Pure engine | `server/automations/consolidation_engine.py` (new) | `suggest_clusters(candidates)` + guardrail helpers. No I/O. |
| Prompt | `server/sessions/pr_fix.py` (extend) | `build_consolidation_prompt(repo, base, prs)`. Pure. |
| Orchestrator | `server/sessions/orchestrator.py` (extend) | `consolidation_candidates(repo)` + `_pr_doctor_consolidate(repo, numbers)`. |
| Provider | `server/automations/action_providers.py` (extend) | Pass-throughs on `PrDoctorActionProvider` + status→level/message map. |
| API | `server/api/automations.py` (extend) | `GET /pr-doctor/consolidation`, `POST /pr-doctor/consolidate`. |
| Frontend | `web/src/components/ConsolidatePanel.tsx` (new) + `automations.ts` + `hooks.ts` | Collapsible panel inside the pr-doctor `AutomationCard`. |

### Why a dedicated surface, not the generic `actionable`/`trigger`

The generic automation-action contract (just shipped, `2026-07-04-automation-actions-surface`) is
strictly **one-item → one-action**: `trigger(item_id: str, kind: str)`. Consolidation is
**many-items → one-action** (`numbers: list[int]`). Forcing it through the single-item endpoint
would distort that contract for one feature — the exact over-generalization to avoid. So
consolidation gets its own two pr-doctor routes and its own provider methods, living *beside* the
generic surface, not bent through it.

## GraphQL / metadata availability

Confirmed against `server/github/pr_status.py`. The shared `_PR_NODE_FIELDS` selection (used by
**both** the per-branch and population queries) and the `PullRequestInfo` dataclass carry:

| Signal | Fetched today? | v1 use |
|--------|----------------|--------|
| number, url, title, state, isDraft | ✅ | identity + open/non-draft guard |
| CI rollup → `checks` (passing/failing/pending) | ✅ | all-green cluster filter + risk flag |
| `head_ref` (headRefName), `mergeStateStatus`, `mergeable` | ✅ | conflict/behind risk flags |
| `updated_at` | ✅ | recency signal |
| **changed file paths** | ❌ | needs `files(first:N){nodes{path}}` — **v1.5** |
| **author** | ❌ | needs `author{login}` — **v1.5** |
| **labels** | ❌ | needs `labels(first:N){nodes{name}}` — **v1.5** |

**v1 recommender uses only already-fetched signals** — so no GraphQL/DTO change is needed to ship:
conventional-commit **title scope** (`fix(deploy): …` → `deploy`), **all-green CI**
(`checks == "passing"`), open/non-draft, and recency.

**v1.5 enrichment** (separate follow-up PR) adds files/author/labels. The gotcha: any new
`PullRequestInfo` field must be threaded through **both** `to_dict` *and* `from_dict` — the
daemon→edge RPC roundtrip silently drops a field present in one but not the other. That, plus the
single shared edit to `_PR_NODE_FIELDS`, is why enrichment is its own PR, not a v1 blocker.

### Candidate source — already free

`self._pr_population.all()` returns `{repo → [PullRequestInfo]}` for **every** open PR per repo
(orchestrator.py:2532, 2678). That is the source for the multi-select list — deliberately broader
than `_pr_doctor_scope_pairs()`, which only yields PRs authored by live sessions. So the picker
needs **zero** new fetching; `consolidation_candidates(repo)` filters that cache to open/non-draft.

## Backend

### Pure engine (`server/automations/consolidation_engine.py`)

```python
@dataclass(frozen=True)
class ConsolidationCandidate:
    number: int
    title: str
    url: str
    checks: str          # "passing" | "failing" | "pending" | "none"
    mergeable: str       # MERGEABLE | CONFLICTING | UNKNOWN
    updated_at: str
    scope: str           # derived: conventional-commit scope from title, e.g. "deploy" ("" if none)
    # v1.5 enrichment (absent in v1): files: tuple[str, ...], author: str, labels: tuple[str, ...]

@dataclass(frozen=True)
class Cluster:
    numbers: tuple[int, ...]     # ≥2, sorted
    rationale: str               # "3 PRs share scope `deploy`, all green"
    risk: str                    # "low" | "risky"  (risky if any member fails CI or conflicts)

def suggest_clusters(candidates: list[ConsolidationCandidate]) -> list[Cluster]: ...

# Guardrails, enforced backend-side before any spawn:
def validate_selection(candidates: list[ConsolidationCandidate]) -> str | None:
    """Return an error status, or None if the selection is consolidatable.
    Rules: ≥2 candidates; all open/non-draft (caller pre-filters); duplicates rejected."""
```

`suggest_clusters()` is **the user-owned decision point** (learning mode). Its signature and tests
are scaffolded; the body — how to weight title-scope vs. all-green vs. recency, and what threshold
makes a cluster worth surfacing — is the ~10 lines that define the feature's "taste."

### Prompt (`server/sessions/pr_fix.py`)

`build_consolidation_prompt(repo, base, prs)` — pure, mirrors the existing `build_pr_fix_prompt` /
`build_conflict_fix_prompt` style. Instructs the session to:

1. `git fetch origin`, create a fresh branch off `origin/<base>` (base = `master`).
2. For each PR (by number): `gh pr checkout` / cherry-pick or merge its commits onto the new branch;
   resolve conflicts by reconciling both sides (don't blindly discard); run tests/lint/build.
3. Open the consolidated PR with a body that lists every original as `Closes #N` and summarizes the
   combined change (per project norm: `Fixes/Closes` keyword so merge auto-closes stragglers).
4. Close each original PR with a `Superseded by #<new>` comment (`gh pr close --comment`).
5. Follow the project completion workflow (branch off `origin/master`, non-draft, auto-merge armed).

### Orchestrator (`server/sessions/orchestrator.py`)

```python
async def consolidation_candidates(self, repo: str | None = None) -> dict:
    """Open, non-draft PRs (from _pr_population.all(), scoped to pr-doctor repos)
    mapped to ConsolidationCandidate dicts, plus suggest_clusters() output, grouped by repo."""

async def _pr_doctor_consolidate(self, repo: str, numbers: list[int]) -> tuple[str, str | None]:
    """Validate selection → resolve ccswitch clone cwd (gitrepos.ensure_clone) →
    build_consolidation_prompt → spawn ONE session via _pr_doctor_ctx.spawn_session with
    dedupe_key f'consolidate:{repo}#{'+'.join(sorted numbers)}'. Returns (status, session_id)."""
```

Reuses `_pr_doctor_ctx.spawn_session(origin_ref=…, dedupe_key=…, repo=…, cwd=…, initial_prompt=…)`
verbatim — same idempotent, account-rotated path PR-doctor fixes already use. The dedupe key means
re-tapping the same set returns the existing session instead of spawning a second.

### Provider (`server/automations/action_providers.py`)

Add to `PrDoctorActionProvider`:

```python
async def consolidation(self) -> dict:                       # → orch.consolidation_candidates()
async def consolidate(self, repo, numbers) -> TriggerResult: # → orch._pr_doctor_consolidate()
```

Status → (level, message) map for consolidation outcomes:

| status | level | message |
|--------|-------|---------|
| `spawned` | ok | "Spawned consolidation session" |
| `duplicate` | warn | "Already consolidating this set" |
| `too_few` | warn | "Select at least 2 PRs" |
| `not_actionable` | warn | "One or more PRs are no longer open" |
| `ceiling` | warn | "All accounts at/above usage ceiling — try later" |
| `clone_failed` | error | "Clone failed — see the automation feed" |
| `spawn_failed` | error | "Spawn failed — see the automation feed" |

### API (`server/api/automations.py`)

```
GET  /api/automations/pr-doctor/consolidation                 → { repos: {repo: {candidates[], clusters[]}} }
POST /api/automations/pr-doctor/consolidate  {repo, numbers[]} → TriggerResult.to_dict()
```

Auth unchanged (`current_identity` / tailnet allowlist). **Malformed** body (missing `repo`,
`numbers` not a list of ints) → 422 at the request layer. **Semantic** rejections are graceful
`TriggerResult`s, not 4xx — `<2 numbers` → `too_few`, an unknown/closed PR in the set →
`not_actionable` — matching the poll-cache-staleness convention the generic trigger already uses.
So `validate_selection` (engine) owns the ≥2 / open / no-duplicates rules; the API only guards JSON
shape.

## Frontend

### Types + API (`web/src/api/automations.ts`)

```ts
export interface ConsolidationCandidate { number: number; title: string; url: string; checks: string; mergeable: string; scope: string }
export interface Cluster { numbers: number[]; rationale: string; risk: 'low' | 'risky' }
export interface ConsolidationView { repos: Record<string, { candidates: ConsolidationCandidate[]; clusters: Cluster[] }> }

export const fetchConsolidation = () => get<ConsolidationView>(`/api/automations/pr-doctor/consolidation`)
export const postConsolidate = (body: {repo: string; numbers: number[]}) =>
  post<TriggerResult>(`/api/automations/pr-doctor/consolidate`, body)
```

### Hooks (`web/src/api/hooks.ts`)

`useConsolidation(enabled)` (query, `staleTime: 10_000`) and `useConsolidate()` (mutation;
`onSuccess` invalidates the consolidation query + the automation's actionable/sessions queries).

### Component (`web/src/components/ConsolidatePanel.tsx`)

A collapsible **"Consolidate PRs"** section inside the pr-doctor `AutomationCard`, below the
actionable-items list, rendered only for `a.id === 'pr-doctor'`. Contents:

- **Suggested-cluster chips** — one per `Cluster`: "Consolidate these 3 · deploy · low risk". Tap
  selects the cluster's numbers into the checkbox state (doesn't fire immediately — user reviews,
  then hits the button). `risky` chips get the red/confirm treatment.
- **Multi-select list** — one checkbox row per candidate: `#num title` + a CI dot (green/red/amber)
  + conflict badge if `mergeable === 'CONFLICTING'`. Per repo (a repo selector or grouped headers
  when >1 repo in scope).
- **Action bar** — "Consolidate N PRs" button, disabled when <2 selected; a mixed-repo selection is
  prevented at the UI level (selection is scoped to one repo group). Result → toast + inline outcome
  badge colored by `level`, reusing the same toast mechanism `<AutomationActions>` uses.

`ConsolidatePanel` is self-contained within `AutomationCard` and does **not** touch `SessionList`,
so the "~16 `@/api/hooks` vi.mock lists" hazard does not apply. Interactive controls live in the
panel's own action group (not nested in a card-open button) per the a11y `nested-interactive` rule.

## Testing

- **Pure engine** `server/tests/test_consolidation_engine.py` (new): `suggest_clusters` groups
  same-scope all-green PRs, excludes singletons, flags a mixed set `risky`, respects the surface
  threshold; `validate_selection` rejects <2 and duplicates.
- **Prompt** test: `build_consolidation_prompt` names every PR number, emits `Closes #N` per
  original and the supersede-close step; pure/no-I/O.
- **Orchestrator/provider** `server/tests/test_pr_consolidation.py` (new, `_FakeOrch` style):
  `_pr_doctor_consolidate` spawns exactly one session with the sorted-numbers dedupe key and the
  consolidation prompt; a repeat call hits the dedupe path (`duplicate`); a <2 selection returns
  `too_few`; a closed-PR set returns `not_actionable`.
- **API** `server/tests/test_pr_consolidation_api.py` (new): auth 401 without allowlist; GET returns
  the candidates+clusters shape; POST returns a `TriggerResult`; 422 on <2 numbers / missing repo.
- **Frontend** `web/src/components/ConsolidatePanel.test.tsx` (new): renders candidate rows +
  cluster chips; a chip selects its numbers; button disabled <2; POST fires with `{repo, numbers}`;
  toast + inline outcome on ok/warn/error; risky chip requires confirm; empty state.

## Files touched

| File | Change |
|------|--------|
| `server/automations/consolidation_engine.py` | new — `ConsolidationCandidate`, `Cluster`, `suggest_clusters`, `validate_selection` |
| `server/sessions/pr_fix.py` | + `build_consolidation_prompt` |
| `server/sessions/orchestrator.py` | + `consolidation_candidates` + `_pr_doctor_consolidate` |
| `server/automations/action_providers.py` | + `consolidation`/`consolidate` on `PrDoctorActionProvider` + status map |
| `server/api/automations.py` | + `GET /pr-doctor/consolidation`, `POST /pr-doctor/consolidate` |
| `web/src/api/automations.ts` | + consolidation types + fetchers |
| `web/src/api/hooks.ts` | + `useConsolidation` / `useConsolidate` |
| `web/src/components/AutomationCard.tsx` | render `<ConsolidatePanel>` for pr-doctor |
| `web/src/components/ConsolidatePanel.tsx` | new |
| `server/tests/test_consolidation_engine.py` | new |
| `server/tests/test_pr_consolidation.py` | new |
| `server/tests/test_pr_consolidation_api.py` | new |
| `web/src/components/ConsolidatePanel.test.tsx` | new |

## Rollout / follow-ups

- **This PR (v1):** engine + prompt + orchestrator + provider + API + panel, with a title-scope /
  all-green recommender using only already-fetched PR data.
- **Follow-up (v1.5):** enrich `_PR_NODE_FIELDS` + `PullRequestInfo` with `files`/`author`/`labels`
  (threaded through `to_dict` **and** `from_dict`), and upgrade `suggest_clusters()` to weight
  shared-files and same-author. No API/UI shape change — the recommender just gets smarter.

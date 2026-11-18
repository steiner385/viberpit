# Product repo lifecycle: add, set primary, retire, move issues

Date: 2026-11-18 · Status: approved design (session a131f63c)

## Why

Product `ghostdriver` has one repo, `steiner385/ghostdriver-mcp` (private, about to
be archived and become read-only). Development moved to `steiner385/ghostdriver`
(public), whose git history was rewritten, so the two repos share no commits. The
new repo is linked to the same GitHub Project (steiner385 #35) and has the same
labels. Issues were re-created, e.g. `ghostdriver-mcp#257` is now `ghostdriver#3`.

The operator needs to re-point the product without losing the old repo's history,
and to make sure no automation writes to the old repo again.

## What exists today

- `PATCH /api/pm/projects/{slug}` with `repo` (from #1959, exposed in the product
  edit dialog) **overwrites the name on the primary `ProjectRepo` row in place** and
  resets its sync state. The old repo is detached from the product: its mirrored
  work items, sessions and PRs no longer belong to any product, and ownership
  checks report it as unregistered.
- Nothing adds a second repo, removes one, or marks one as read-only.
- `gitrepos.ensure_clone` reuses whatever directory is at `clone_path(owner/name)`
  without checking that directory's `origin`.

## Goals

1. An API, the Products UI and an assistant tool (with a confirmation card) that
   **add** a repo to a product, **set the primary**, and **retire** (or un-retire)
   a repo.
2. **Retired** means: no new sessions, Backlog Runner claims or queue entries, no
   issue reminders, no ViberPit-authored comments, issues or pm writes against
   that repo. Its sessions, PRs and mirrored work items stay visible read-only.
   Nothing is deleted.
3. Everything that derives its target from the product's repos follows the
   current primary: Backlog Runner, roadmap, milestones, dispatch defaults and
   reminders.
4. A session on the new repo always gets a fresh base clone of that repo's own
   URL. An old clone is never reused or re-pointed, even when its directory name
   collides with the new repo's.
5. **Moved-issue map:** the operator records old→new ref pairs, and ViberPit
   migrates the ViberPit-owned state from the old issue to the new one.

## Non-goals and anti-goals

- No deletion of `ProjectRepo` rows, work items, sessions, dispatch history or
  clones. "Detach" means retire.
- No `git remote set-url` on any clone, ever.
- No automatic detection of GitHub's `archived` flag in v1. Retiring is an explicit
  operator action, though the UI says when GitHub reports the repo as archived.
- Live sessions already running on the retired repo are not killed. The rule only
  stops new work.
- Not copying the GitHub-side state of an issue (labels, comments). The new issues
  already exist with their labels; the map migrates ViberPit's overlay only.

## Approaches considered

- **A. Lifecycle columns on `ProjectRepo` (chosen).** Add `retired_at`,
  `retired_reason` and `superseded_by`, plus endpoints for add, set primary,
  retire and un-retire. The old repo stays registered to the product, so ownership,
  history views and audit keep working. Every consumer of "the product's repos"
  splits into *active repos* (automation) and *all repos* (history).
- **B. Keep the in-place repoint, plus a `former_repos` list on `Project`** (like
  `former_slugs`). Smaller, but the old repo stops being a registration. Ownership
  checks would call it unowned, and an unowned repo is exactly what the fleet
  refuses to reason about. History views would need a second lookup path.
  Rejected.
- **C. Register the archived repo as its own "ghostdriver-archive" product.**
  Needs no new state, but splits one product's history across two roadmap
  entries, and gives a dead repo a product-level policy that someone could turn
  on. Rejected.

## Design

### Data

`ProjectRepo` gains three nullable columns, which reconcile ALTER-ADDs:

| column | meaning |
|---|---|
| `retired_at` | when the repo was retired; NULL means active |
| `retired_reason` | free text, e.g. "archived; moved to steiner385/ghostdriver" |
| `superseded_by` | `owner/name` of the repo that replaced it (display and redirect hint) |

A new table, `moved_issue_refs`, holds the moved-issue map:

| column | meaning |
|---|---|
| `id` | primary key |
| `project_id` | the product |
| `old_ref` | e.g. `steiner385/ghostdriver-mcp#257`; unique |
| `new_ref` | e.g. `steiner385/ghostdriver#3` |
| `created_by`, `created_at` | who recorded the pair, and when |
| `migrated_at` | NULL until the overlay was copied |
| `migration` | JSON detail of what moved |

Invariants:

- **Exactly one active primary.** A retired repo cannot be primary, and retiring
  the primary requires naming the new primary in the same call.
- **At least one active repo** per product.

Add migration `0112` for both changes.

### Single source of truth

The new module `server/pm/product_repos.py` holds the predicates and helpers.
Nothing re-implements these:

- `ACTIVE` is the SQL predicate `ProjectRepo.retired_at.is_(None)`.
- `active_repos(db, project_id)` and `primary_repo(db, project_id)`, with sync
  twins for the mirror store.
- `retired_repos_snapshot()` is a process-local TTL cache (about 30 s, cleared on
  any lifecycle write in the same process) that returns the casefolded set of
  retired repos. It is the cheap check for hot paths that have no DB session.
- `is_retired(repo)` is a membership test against that snapshot.

### Where retirement is enforced

| surface | change |
|---|---|
| `mirror_store.list_product_views` (feeds the Backlog Runner queue, needs-you hygiene, reconcile, `queue_inputs`, pm_sync board/doc reads) | `repos` lists **active** repos only, primary first. A retired repo drops out of every automation queue in one place. |
| `_pm_sync_list_repos` (mirror loop) | Skip retired repos: the mirror is frozen at retirement and the history stays as last mirrored. |
| `owning_projects_by_repo` / `resolve_repo_ownership` | Unchanged. A retired repo is still owned by the product, which keeps history attribution intact. |
| `require_mutation_owner` (every pm write: issue disposition, deps, materialize, board status, triage accept) | Raises `OwnershipConflict("retired", …)` for a retired repo. |
| `orchestrator.create_session` (every new session: manual, Backlog Runner, PR Doctor, onboarding, intake) | Refuses a `repo` (or a `cwd` resolving to one) that is retired, with `RepoRetired`, which the API maps to 409. Resuming an existing session is unaffected. |
| `ci_issues.create_issue` / issue-comment helpers (API, assistant, issue filing, CI doctor) | Refuse a retired repo. The API returns 409. |
| primary-repo readers (`primary_repo_for_slug`, `pm_milestones`, `milestone_refresh`, `map_changes._primary_repo`, `capacity_wiring`, pm_onboard spawns, `pm_classify_run`, `delivery_health_refresh`) | Routed through `product_repos.primary_repo`. Since a retired repo is never primary, these follow the new primary with no per-site logic. |
| roadmap / product detail / health / triage list / `_needs_you` (history) | Keep **all** repos so history stays visible, but `_needs_you` and triage "needs home" queries use **active** repos. |
| Web dispatch defaults (`NewInitiativeDialog`, Roadmap/Milestones pages, Capability doc link) | Use the primary or the first active repo, never `repos[0]` blindly. |

### Clones

`clone_path` is unchanged for the normal case. A new step in `ensure_clone`
checks an existing clone at that path: if its `origin` URL does not normalize to
the requested `owner/name`, `ensure_clone` **does not reuse it, fetch it or
re-point it**. It logs `clone.origin_mismatch` and clones fresh into a sibling
path, `<owner>/<name>@<sha8(owner/name casefolded)>`, which is stable, so later
calls find it again. `clone_path` learns the same rule, so readers that locate
clones (branch ownership, picker "cloned", move) agree with `ensure_clone`.

The mismatched directory is left untouched, because live worktrees may point
into it. `find_local_reference` already matches by origin URL, so `--reference`
can never pull objects from the unrelated old repo.

### Moved-issue map: map and migrate

`POST /api/pm/projects/{slug}/moved-refs` takes `{pairs: [{old, new}]}`:

- Validation: the old ref must belong to one of the product's repos, the new ref
  to an active one, and no pair may chain.
- Pairs are stored (upsert on `old_ref`).
- Each pair is then migrated idempotently once **both** work items are mirrored;
  the response reports pairs still `pending` until then.
- `GET` lists the pairs and their status.
- `GET …/moved-refs/suggest?from=<repo>&to=<repo>` proposes pairs by exact title
  match (case and whitespace folded) between the old repo's open issues and the
  new repo's issues. These are suggestions only and are never auto-applied.

Migration of one pair, in one transaction:

1. **WorkItem overlay** (holds, feature link, scores, triage state, estimates):
   copied old→new where the new item's field is empty. The old item's hold is
   released with signal `moved`, so the old ref no longer shows as held.
2. **`WorkItemDep`**: rewrite `src_ref`/`dst_ref` (and their work-item ids) from
   old to new. Where the rewritten edge already exists, the old row is
   deactivated (`active = False`) instead of duplicated.
3. **`MilestoneNote.ref`** and **`OwnerRecommendation.target_ref`** for open
   rows: rewritten.
4. **Backlog groups**: the current generation is invalidated, so the grouper
   re-plans from the active queue.
5. An audit row `pm.moved_ref.migrate` records the pair and what moved.

Pending pairs are retried by the pm_sync pass after the new repo's mirror
advances. The map is also read by `milestone_def.parse_ref` and by blocker
resolution, so a milestone definition or issue body still naming
`ghostdriver-mcp#257` resolves to `ghostdriver#3`.

Not migrated: `DispatchOutcome` and Backlog Runner claims. They are dispatch
history and live locks, and they end with their sessions. The retired repo's
items have already dropped out of the queue, so nothing new claims them.

### API

All endpoints are under `/api/pm/projects/{slug}/repos` and are audited
(`pm.project.repo.*`). They take the registration lock, and `add` runs the
ownership preflight.

- `POST /repos`, body `{repo, makePrimary?}`: add an active repo.
- `POST /repos/{owner}/{name}/primary`: make an active repo the primary.
- `POST /repos/{owner}/{name}/retire`, body `{reason?, supersededBy?,
  newPrimary?}`: retire a repo. `newPrimary` is required when retiring the
  primary.
- `POST /repos/{owner}/{name}/unretire`: make the repo active again.

The legacy `PATCH {repo}` keeps its contract, but is re-implemented as "add new
repo, make it primary, retire the old one (reason `repointed`)" instead of an
in-place overwrite. Nothing is lost silently.

`_repo_to_dict` gains `retiredAt`, `retiredReason` and `supersededBy`, and
`isArchived` when known.

### Assistant

A new operator-tier MUTATION tool, `update_product_repos(slug, action, repo,
reason?, supersededBy?, newPrimary?)`. Each handler calls exactly one of the
endpoints above. Its card renderer shows `"<action> <repo>"` plus "primary:
before → after". Retire has destructive impact, so it is a MUTATION whose card
always appears, carrying the expected-before primary. The endpoint refuses with
409 if the primary has moved since the card was shown.

A second tool, `map_moved_issues(slug, pairs)`, wraps the moved-refs `POST`, and
its card lists the pairs. Both tools are added to the route allowlists, and eval
cases are added.

### UI (Products → product detail → Repositories)

The repo list becomes a small management panel. Each row shows the repo name, a
primary badge, a backfill state, a "Retired · <reason> · → superseded_by" muted
style, and actions: *Make primary*, *Retire…*, *Un-retire*. Below the list are an
"Add repository" input and a "Moved issues…" dialog that lists the pairs, offers
the title-match suggestions with checkboxes, and posts the selected pairs. The edit
dialog's "Primary repository" field is removed in favor of this panel.

## Left to the builder

- Exact placement of each enforcement call, as long as each surface in the table
  goes through `product_repos`.
- Exact UI layout within the existing ProductDetail / dialog primitives.
- The cache TTL value.

## Operator steps for ghostdriver (go in the PR body)

1. Products → ghostdriver → Repositories → Add `steiner385/ghostdriver`, then
   make it primary.
2. Retire `steiner385/ghostdriver-mcp` with reason "archived", superseded by
   `steiner385/ghostdriver` (moved-issue pairs need the old repo retired first).
3. Wait for the first backfill of the new repo, then open Moved issues…, accept
   the suggestions and add any missing pairs (e.g. `#257 → #3`), then Apply.
4. Archive `ghostdriver-mcp` on GitHub.
5. Set `max_in_flight` back above 0 in the product policy.

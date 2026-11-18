---
slug: product-repo-lifecycle
parent: planning-and-automation
title: Product Repo Lifecycle
state: live
links:
  specs:
    - docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md
  code:
    - server/pm/product_repos.py
    - server/pm/repo_lifecycle.py
    - server/pm/moved_refs.py
    - server/api/pm.py
    - src/viberpit/gitrepos.py
    - server/assistant/tools.py
    - web/src/features/roadmap/detail/ProductReposPanel.tsx
    - web/src/features/roadmap/detail/MovedIssuesDialog.tsx
---

# Product Repo Lifecycle

Add a repository to a product, make it the primary, retire the old one, and carry the old
repo's issue state across, without losing history and without any automation writing to the
retired repo again.

Nothing is ever deleted: "detach" means **retire**. Every change writes an audit event
(`pm.project.repo.add|primary|retire|unretire`, `pm.moved_ref.record|migrate`).

## Retired repos

A repo is retired when `ProjectRepo.retired_at` is set (with `retired_reason` and
`superseded_by`). `server/pm/product_repos.py` is the single source of truth; every path asks
`is_retired` / `require_active` / `active_repos` / `primary_repo` instead of reading rows.

**Blocked for a retired repo:** new sessions, Backlog Runner claims and queue entries, issue
filing, ViberPit-authored issue comments, pipeline and repo writes, pm sync targets, and
primary selection (a retired repo can never be primary). A refused write is HTTP 409
`{"error": "repo_retired", "message": ...}` (also carrying `supersededBy` in the daemon RPC
envelope).

**Stays visible:** the repo's mirrored work items, sessions, PRs and clones. Sessions already
running are not killed; the rule only stops new work. `is_retired` answers from a 30 s process
snapshot and is fail-open on a DB error.

## Invariants and error codes

Exactly one active primary per product; a retired repo is never primary; at least one repo
stays active. All refusals happen before any write.

| Code | Status | When |
|---|---|---|
| `not_found` | 404 | repo is not attached to the product |
| `repo_retired` | 409 | set-primary on a retired repo |
| `primary_changed` | 409 | `expectedPrimary` differs from the live primary |
| `open_map_changes` | 409 | changing the primary while a map-changes branch/PR is open |
| `ownership_conflict` | 409 | add of a repo already registered to another product |
| `last_active_repo` | 422 | retiring the only active repo |
| `new_primary_required` | 422 | retiring the primary without `newPrimary` |
| `invalid_new_primary` | 422 | `newPrimary` is not another active repo (or, for a non-primary, not the current primary) |

Adding a repo already on this product reuses its row (un-retiring it if needed). Un-retiring
restores the repo to queues and sync; it becomes primary only when the product has no active
primary. Retire and re-retire are idempotent.

The legacy `PATCH /api/pm/projects/{slug}` field `repo` no longer overwrites the primary's name:
it adds the new repo as primary and **retires** the old one, which keeps its mirrored history.

## Clone collision rule

A session on a repo always gets a clone of that repo's own URL (`src/viberpit/gitrepos.py`,
`_resolve_clone`). If `<owner>/<name>` already exists with an `origin` that is a different repo
(a moved repo with unrelated history), it is **never reused, re-pointed or modified**; the
session clones into the sibling `<owner>/<name>@<sha8>` (`collision_clone_path`, sha1 of the
casefolded `owner/name`, deterministic so every caller agrees with no stored state). The
mismatch is logged at INFO.

## Moved-issue map

`server/pm/moved_refs.py` records old to new issue refs (`owner/name#N`) in `MovedIssueRef` and
migrates ViberPit-owned state.

- **Old repo must be retired first**; the new ref's repo must be active. An active repo's issue
  is never reachable, because migration releases holds. No duplicate old ref, no cross-product
  old ref, no chains. A migrated pair cannot be re-pointed (`already_migrated`).
- **What migrates** (into empty slots only, idempotent): overlay fields (feature, RICE, triage
  state), the hold, dependency edges (rewritten to the new ref; duplicates deactivated), open
  milestone notes, pending owner recommendations; old-ref backlog groups are dropped.
- **Decision holds move with their proposal.** If the new item is already held, already has a
  pending proposal, or the old hold has no pending proposal, both stay on the old item
  (`decision_kept_on_old`).
- **Pending pairs.** A pair whose old or new work item is not mirrored yet is stored `pending`
  and retried by pm_sync after each mirror advance (one savepoint per pair). A pair whose old
  repo is un-retired afterwards stays pending and writes nothing.
- **Suggestions** pair open issues of the old repo with issues of the new one by exact title
  (case and whitespace folded); a title shared by two issues on either side, or an already
  mapped ref, is skipped. Suggestions write nothing.
- Recording releases holds, so agent-class callers (sessions, unattributed loopback) are refused
  with 422 `hold_not_permitted`; a person, or the assistant on its operator's confirmed card, may.

## API (`/api/pm`)

| Route | Body |
|---|---|
| `POST /projects/{slug}/repos` | `repo`, `makePrimary?` (201) |
| `POST /projects/{slug}/repos/{owner}/{name}/primary` | `expectedPrimary?` |
| `POST /projects/{slug}/repos/{owner}/{name}/retire` | `reason?`, `supersededBy?`, `newPrimary?`, `expectedPrimary?` |
| `POST /projects/{slug}/repos/{owner}/{name}/unretire` | none |
| `GET /projects/{slug}/moved-refs` | list pairs |
| `POST /projects/{slug}/moved-refs` | `pairs: [{old, new}]`; 422 `bad_pairs` lists each bad pair |
| `GET /projects/{slug}/moved-refs/suggest?from=&to=` | 422 `bad_repo` unless `from` is a repo of the product and `to` is active |

Lifecycle routes return the product detail.

## Assistant tools (operator tier)

Both are mutations behind a confirmation card (`server/assistant/tools.py`), each one call to the
routes above.

- `update_product_repos(product, action, repo, ...)`: `add` (`makePrimary`), `set_primary`,
  `retire` (`reason`, `supersededBy`, `newPrimary`), `unretire`. `set_primary` and `retire`
  require `expectedPrimary`, read off `get_product` just before; the card shows the expected to
  new primary and the server refuses on a mismatch (`primary_changed`).
- `map_moved_issues(product, pairs)`: records and migrates up to the batch maximum; the card lists
  each `old -> new`.

## UI

Roadmap product detail, `ProductReposPanel`: per-repo Make primary, Retire... (reason, superseded
by, new primary when retiring the primary) and Un-retire; an Add repository field; and
Moved issues... (`MovedIssuesDialog`: stored pairs, title-match suggestions to accept, manual
old/new pairs).

## Runbook: Moving a product to a new repository

1. **Optional: pause the product's Backlog Runner** (`max_in_flight=0`) while moving.
2. **Add the new repo** to the product (panel or `POST .../repos`).
3. **Make it primary.** Passing `makePrimary` on add does both. Merge or abandon any open
   map-changes PR first.
4. **Retire the old repo**, giving a reason and `superseded by` the new repo. If you skipped
   step 2 and the old repo is still primary, pass `newPrimary` here. From now on nothing new is created there.
5. **Wait for the first backfill** of the new repo, so its issues are mirrored. (Pairs recorded
   early are kept `pending` and migrate after the mirror catches up.)
6. **Moved issues...**: accept the title-match suggestions, then add the missing pairs by hand
   (retitled or duplicated issues are not suggested).
7. **Archive the old repo on GitHub.** ViberPit does not detect GitHub's archived flag; retiring
   is the explicit signal.
8. **Re-enable automation**: set `max_in_flight` back above 0 and confirm automation now targets the new repo.

Rollback: un-retire the old repo and make it primary again. Migrated state is not moved back.

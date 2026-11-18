# Product Repo Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator add a repo to a product, set the primary, retire a repo (read-only history,
no new automation against it), and map moved issues old→new with migration of ViberPit-owned state.

**Architecture:** Lifecycle columns on `ProjectRepo` + a `MovedIssueRef` table; one module
(`server/pm/product_repos.py`) owns "active / primary / retired" and every consumer routes through it.
Enforcement sits at the shared choke points (product views, mutation-owner guard, session creation,
issue/comment helpers). Clones gain an origin check so a colliding directory is never reused.

**Tech Stack:** FastAPI, SQLAlchemy (SQLite + Postgres), pytest; React + TS + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md`

## Global Constraints

- New model columns nullable (reconcile ALTER-ADDs them); new table: every non-key column nullable.
- Migration `0112_product_repo_lifecycle.py`, `down_revision` = the `0111_session_sandbox` revision id.
- New MODEL ⇒ add `MovedIssueRef` to `_REQUIRED_EXTRAS` in `server/tests/test_postgres_compat.py` if it has NOT NULL columns besides the PK (prefer none) and check `test_schema_reconcile_coverage.py`.
- Timestamps via `server/db/clock.py:db_utc_now()` in SQL; never `func.now()`.
- Audit types: `pm.project.repo.add`, `pm.project.repo.primary`, `pm.project.repo.retire`, `pm.project.repo.unretire`, `pm.moved_ref.record`, `pm.moved_ref.migrate`.
- Errors: retired-repo refusals are HTTP 409 `{"error": "repo_retired", "message": ...}`.
- No `ccswitch` spellings; no new `CCSWITCH_*` settings.
- Server tests: run with `env -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1`, sessiond/daemon-role vars unset, main checkout venv python; `src/viberpit` edits need `PYTHONPATH=$PWD/src`.
- Lint gate: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`; web: `npm run typecheck && npm run lint && npm run test && npm run build`.

## Review Focus

1. Retiring the primary without naming a new primary → 422, nothing changes (Task 2 test).
2. Adding a repo already registered to ANOTHER product → 409 ownership_conflict, not a silent second owner (Task 2 test).
3. A clone dir at `<owner>/<name>` whose origin is a different repo → fresh sibling clone, old dir untouched, `clone_path` agrees (Task 4 test).
4. A moved-ref pair whose new work item is not mirrored yet → stored `pending`, migrated on a later pass, idempotent on re-run (Task 5 test).
5. Un-retiring a repo restores it to queues/sync, and a retired repo can never be made primary (Task 2 test).

---

### Task 1: Schema + `product_repos` source of truth

**Files:**
- Modify: `server/db/models.py` (`ProjectRepo` ~L2057; add `MovedIssueRef` after `WorkItemDep`)
- Create: `server/db/migrations/versions/0112_product_repo_lifecycle.py`
- Create: `server/pm/product_repos.py`
- Modify: `server/pm/repo_project.py:129` (`primary_repo_for_slug` → excludes retired; delegates)
- Test: `server/tests/test_product_repos.py`

**Interfaces — Produces:**
- `ProjectRepo.retired_at: datetime|None`, `.retired_reason: str|None`, `.superseded_by: str|None`
- `MovedIssueRef` (`moved_issue_refs`): `id`, `project_id`, `old_ref` (unique, indexed), `new_ref`, `created_by`, `created_at`, `migrated_at`, `migration: dict|None`
- `product_repos.ACTIVE` (SQL clause), `async active_repos(db, project_id) -> list[str]` (primary first), `async primary_repo(db, project_id) -> str|None`, `active_repos_sync(db: Session, project_id) -> list[str]`, `primary_repo_sync(db, project_id) -> str|None`, `async retired_repo_set(db) -> frozenset[str]` (casefolded), `is_retired(repo: str) -> bool` (process TTL cache, 30 s, sync DB read via `server.db` sync session factory), `invalidate_retired_cache() -> None`, `class RepoRetired(ValueError)` with `.repo`.

- [ ] Step 1: tests — `test_active_repos_excludes_retired_primary_first`, `test_primary_repo_ignores_retired_row`, `test_is_retired_cache_invalidated`, `test_primary_repo_for_slug_skips_retired`.
- [ ] Step 2: run, expect FAIL (import error).
- [ ] Step 3: add columns, model, migration, module; `primary_repo_for_slug` adds `ACTIVE`.
- [ ] Step 4: run tests + `server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py` → PASS.
- [ ] Step 5: commit `feat(pm): repo lifecycle columns, moved-ref table, product_repos helpers`.

### Task 2: Lifecycle service + API

**Files:**
- Create: `server/pm/repo_lifecycle.py`
- Modify: `server/api/pm.py` (`_repo_to_dict`; new routes; `update_project` repo branch ~L537-556)
- Test: `server/tests/test_api_project_repos.py`

**Interfaces — Consumes:** Task 1. **Produces:**
- `async add_repo(db, project, repo, *, make_primary: bool, snapshot) -> ProjectRepo` (raises `OwnershipConflict`)
- `async set_primary(db, project, repo, *, expected_primary: str|None = None) -> None` (409 `primary_changed` when expected mismatches; 409 `repo_retired` for a retired target)
- `async retire_repo(db, project, repo, *, reason, superseded_by, new_primary, expected_primary=None) -> None` (422 `new_primary_required` when retiring the primary w/o `new_primary`; 422 `last_active_repo` when it would leave none active)
- `async unretire_repo(db, project, repo) -> None`
- Each calls `invalidate_retired_cache()` and records the audit type.
- Routes (all `/api/pm/projects/{slug}/repos…`): `POST ""` `{repo, makePrimary?}` → 201 detail dict; `POST /{owner}/{name}/primary` `{expectedPrimary?}`; `POST /{owner}/{name}/retire` `{reason?, supersededBy?, newPrimary?, expectedPrimary?}`; `POST /{owner}/{name}/unretire`. All return `_project_detail_dict`. Lock + ownership preflight order exactly as `update_project` does it. Refused while a map-changes PR is open if the primary changes (same 409 `open_map_changes`).
- `_repo_to_dict` adds `retiredAt`, `retiredReason`, `supersededBy`.
- `PATCH {repo}` becomes add(make_primary) + retire(old, reason `"repointed"`, superseded_by new); response shape unchanged; existing PATCH tests must still pass (update expectations that assumed in-place overwrite to the retained-old-row behaviour).

- [ ] Step 1: tests — add/primary/retire/unretire happy paths; Review Focus 1, 2, 5; `test_patch_repo_retains_old_repo_as_retired`; audit rows written.
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS incl. `server/tests/ -k "project and (patch or update or repo)"`.
- [ ] Step 5: commit `feat(pm): add/primary/retire/unretire product repos API`.

### Task 3: Enforce retirement + follow the primary

**Files (modify):** `server/pm/mirror_store.py:list_product_views`; `server/sessions/orchestrator.py` (`_pm_sync_list_repos` ~L12289, `create_session` ~L4323, primary readers L13827/13935/14067, `_backlog_seed_policies` L10874 use ACTIVE); `server/pm/repo_project.py:require_mutation_owner`; `server/ci_issues.py` (or wherever `create_issue` / issue-comment helper live) ; `server/api/repos.py` (map `RepoRetired` → 409); `server/api/pm_milestones.py`, `server/pm/milestone_refresh.py`, `server/pm/map_changes.py:_primary_repo`, `server/automations/capacity_wiring.py`, `server/pm/delivery_health_refresh.py`, `server/pm/session_triage.py:_needs_you` (active), `server/automations/decision_escalation.py` → route through `product_repos.primary_repo`/`ACTIVE`.
- Test: `server/tests/test_repo_retirement_enforcement.py`

**Consumes:** Task 1 helpers, `RepoRetired`.
- `create_session`: before clone, `if repo and is_retired(repo): raise RepoRetired(repo)`; also when `cwd` given, resolve `gitrepos.repo_at_path(cwd)` only if `repo is None` and the call is automation (`identity == SYSTEM_IDENTITY`) — keep cheap. Resume paths untouched.
- `require_mutation_owner`: after registration check, refuse `OwnershipConflict("retired", "<repo> is retired; use <superseded_by>")`.
- `list_product_views`: active repos only, primary first.
- `_pm_sync_list_repos`: skip retired.

- [ ] Step 1: tests — `test_product_views_drop_retired`, `test_pm_sync_skips_retired`, `test_mutation_owner_refuses_retired`, `test_create_session_refuses_retired_repo`, `test_create_issue_api_refuses_retired_repo` (409), `test_comment_api_refuses_retired_repo`, `test_primary_readers_follow_new_primary` (milestone primary + `primary_repo_for_slug` after set_primary).
- [ ] Step 2 FAIL → Step 3 implement → Step 4 PASS + `pytest server/tests/ -k "backlog or pm_sync or milestone or needs_you" -n 4`.
- [ ] Step 5: commit `feat(pm): retired repos get no dispatch, claims, writes or sync`.

### Task 4: Clone origin check

**Files:** Modify `src/viberpit/gitrepos.py` (`clone_path` L378, `ensure_clone` L765). Test: `tests/test_gitrepos_origin_collision.py`.
**Produces:** `clone_origin_matches(path: Path, name_with_owner: str) -> bool` (missing origin ⇒ True only if dir has no `.git`; normalized URL compare, case-insensitive); `collision_clone_path(base_dir: Path, name_with_owner) -> Path` = `<owner_dir>/<name>@<sha1(owner/name casefold)[:8]>`.
- `clone_path`: when the resolved existing dir has a `.git` whose origin mismatches, return `collision_clone_path` instead (existing-or-new). `ensure_clone` logs `clone.origin_mismatch` at INFO with both repos and proceeds with the collision path. Never `remote set-url`, never move/delete the old dir.
- [ ] Step 1: tests with real temp git repos (`git init` + `remote add origin`): Review Focus 3; matching origin reused; case-variant still reused; `@hash` path stable across calls.
- [ ] Step 2 FAIL → 3 implement → 4 `PYTHONPATH=$PWD/src pytest tests/test_gitrepos*.py tests/test_clone*.py` PASS.
- [ ] Step 5: commit `fix(clones): never reuse a clone whose origin is another repo`.

### Task 5: Moved-issue map + migration

**Files:** Create `server/pm/moved_refs.py`; modify `server/api/pm.py` (routes), `server/pm/milestone_def.py:parse_ref` callers (via `milestone_refresh`/`pm_sync` milestone read: pass `moved: Mapping[str,str]` and apply `resolve_moved`), `server/sessions/orchestrator.py` (pm_sync pass: retry pending after `_pm_sync_save_state`), `server/pm/holds.py` (`RELEASE_SIGNALS` += `"moved"`). Test: `server/tests/test_moved_refs.py`.
**Produces:**
- `validate_pairs(db, project_id, pairs) -> list[tuple[str,str]]` (old ∈ product repos, new ∈ active repos, refs match `ISSUE_REF_RE`, no chain old→new→…, no duplicate old; raise `ValueError` → 422 `bad_pairs` with per-pair messages)
- `record_pairs(db, project_id, pairs, *, actor) -> list[MovedIssueRef]` (upsert on old_ref)
- `migrate_pair(db: Session, row: MovedIssueRef, *, now) -> dict|None` (sync; None if either WorkItem missing ⇒ pending). Copies overlay (`feature_id, reach, impact, effort, confidence, triage_state, classified_by, classified_at, hold_*`) where new's field is NULL; clears old hold via `clear_hold` w/ signal `moved`; rewrites `WorkItemDep` src/dst ref+id (dedupe → deactivate old); rewrites open `MilestoneNote.ref` / `OwnerRecommendation.target_ref`; deletes rows of the current `BacklogGroup` generation containing old ref; stamps `migrated_at`, `migration` detail.
- `migrate_pending(db: Session, *, project_id=None, now) -> int`
- `moved_map(db) -> dict[str,str]`; `resolve_moved(ref: str, moved: Mapping[str,str]) -> str`
- `suggest_pairs(db, from_repo, to_repo) -> list[{old, new, title}]` (old repo OPEN issues; exact title match after casefold + whitespace collapse; skip already-mapped; ambiguous titles skipped)
- Routes: `GET /projects/{slug}/moved-refs` → `{pairs:[{old,new,status:"migrated"|"pending",migratedAt,migration}]}`; `POST` `{pairs:[{old,new}]}` → records + migrates now, returns same shape; `GET /moved-refs/suggest?from=&to=`.
- [ ] Step 1: tests — overlay+hold copy, dep rewrite + dedupe, Review Focus 4, chain rejected, suggest matching, milestone ref resolution through map.
- [ ] Step 2 FAIL → 3 → 4 PASS. Step 5 commit `feat(pm): moved-issue map migrates holds, deps, notes`.

### Task 6: Assistant tools

**Files:** `server/assistant/tools.py` (ToolSpec, `OPERATOR_TOOLS`, `CARD_ITEMS`, handlers, registry list ~L2620), `server/assistant/policy.py` (`OPERATOR_ROUTE_ALLOW`), eval cases under `docs/superpowers/evals/assistant/`, tests `server/tests/test_assistant_product_repo_tools.py` (+ update any tool-count/allowlist parity tests).
- `update_product_repos(slug, action: "add"|"set_primary"|"retire"|"unretire", repo, reason?, supersededBy?, newPrimary?, makePrimary?)` MUTATION, operator tier; reads `GET /api/pm/projects/{slug}` ONLY in the card renderer? NO — handler calls exactly one endpoint; it sends `expectedPrimary` from args (`expectedPrimary` required arg the model fills from get_product). Card: `"<action> <repo>"`, `"primary: <expectedPrimary> → <after>"`, reason.
- `map_moved_issues(slug, pairs)` MUTATION, operator tier → `POST …/moved-refs`; card lists `old → new`.
- [ ] tests: handler hits the right route/body; card items; policy allowlist contains the routes; non-operator token refused. Commit `feat(assistant): product repo lifecycle + moved-issue tools`.

### Task 7: Web UI

**Files:** `web/src/api/pm.ts` (types `retiredAt/retiredReason/supersededBy`; hooks `useAddProjectRepo`, `useSetPrimaryRepo`, `useRetireRepo`, `useUnretireRepo`, `useMovedRefs`, `useRecordMovedRefs`, `useSuggestMovedRefs`; drop `repo` from `UpdateProjectInput`), create `web/src/features/roadmap/detail/ProductReposPanel.tsx`, `web/src/features/roadmap/detail/MovedIssuesDialog.tsx`, modify `ProductDetail.tsx` (render panel instead of the plain list), `EditProductDialog.tsx` (remove repo field), `NewInitiativeDialog.tsx`, `RoadmapPage.tsx`, `MilestonesPage.tsx` (primary → first ACTIVE fallback, via one helper `primaryRepoOf(project)` in `web/src/api/pm.ts`). Tests alongside (`*.test.tsx`). grep `web/e2e` for changed strings ("Primary repository").
- [ ] tests: panel renders retired muted with "→ superseded"; retire dialog requires new primary when retiring primary; moved dialog posts selected suggestions; `primaryRepoOf` skips retired. Full `npm run test`. Commit `feat(web): manage product repositories and moved issues`.

### Task 8: Docs

- Update `docs/product/features/` (new `product-repo-lifecycle.md`), mention in `deploy/README.md` nothing. Commit with spec+plan.

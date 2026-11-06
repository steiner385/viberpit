# Desktop-parity D-22: configurable branch prefix behind recorded ownership — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (owner decision D22b, 2026-11-06).**
- The session-branch prefix becomes a setting in Settings › Sessions.
- Every branch ViberPit creates is **recorded in the DB**, and automatic `git branch -D` requires that record instead of a matching name alone.
- The built-in prefixes (`viberpit/`, legacy `ccswitch/`) keep matching older branches.
- A prefix that clashes with an existing branch namespace is refused.

**Why ownership first.** `brand.is_session_branch` (a plain `startswith`) is today the only gate on `git branch -D` in `remove_worktree`. With a configurable prefix, a value like `feature/` would make the user's own branches deletable. Recording ownership makes deletion depend on proof that ViberPit created the branch, so the name stops being the safety check. This is safer than today even with the prefix left at its default.

## Facts (origin/master `9aa96cdc`)

**Matcher and name builders.**
- The prefix constants are at `src/viberpit/brand.py:75-84`, and `is_session_branch` at `:87-98`.
- The builders are `worktrees._child_branch(base, sid)` (`:517`), which gives `viberpit/<base>-<short>`, and `fork_branch_name` (`:820`, scanned by `_next_fork_branch` `:826`), which gives `viberpit/fork-<short>-<n>`.
- The web mirror is `web/src/brand.ts:52-57`.
- The ratchet entry `branch-prefix-dual-match` (`.brand-ratchet.toml:213`) is permanent.

**Creation sites.**
- `create_worktree` paths 1 and 3 (`worktrees.py:718-726`, `:739-746`; `is_child=True`). Path 2 checks out the **user's own** branch.
- `create_fork_worktree` (`:1066`).
- `worktree_transfer.import_` (`:286`, `branch -f`; `prev` is None when it is new).
- The server callers are spawn (`orchestrator.py:6028`), fork (`_make_fork_worktree` `:19524`) and move import (`_transfer_import` `:23501`).
- `recreate_worktree` never creates a branch.

**Deletion.**
- The **only** session-branch `git branch -D` is in `remove_worktree` (`worktrees.py:1552-1561`). It is gated by `is_session_branch` plus the unpushed and snapshot guards.
- The branch name comes from the worktree's live HEAD.
- Callers:
  - janitor (`worktrees_janitor.py:319`, `:389`), whose loop is at `orchestrator.py:28814`;
  - Discard (`orchestrator.py:7110`);
  - close and spawn rollback (`_cleanup_session_paths` `:7166-7183`);
  - transfer finalize (`worktree_transfer.py:383`, callers `orchestrator.py:23486`, `:23590`).
- `_discard_fork` and the `import_` rollback only delete a branch that the same call just created. No remote branch delete exists.

**Non-destructive matcher uses.**
- The `sessionBranch` preflight flag (`orchestrator.py:6918`) feeds the Discard dialog's "will be deleted / kept" copy (`DiscardWorktreeDialog.tsx:183`).
- `branchDeleted` (`:7115`).
- The read-only prefix row in `SessionDefaultsSection.tsx:285`.

**DB.**
- The CLI core (`src/viberpit/`) has no DB access. Ownership must be resolved in the server and passed down, following the `_fetch_archived_cwds` → `live_cwds` precedent (`orchestrator.py:28798`).
- There is **one shared Postgres** for all hosts, but clones are per host, so a record must be host- and clone-scoped.
- The newest migration was `0107_milestone_notes` when written; D-22 landed as `0109_session_branches` and `0110_app_settings_branch_prefix` after `0108_session_snooze`.
- A new table needs entries in:
  - `_REQUIRED_EXTRAS` (`server/tests/test_postgres_compat.py`);
  - `_FK_LINKS` if it has an FK;
  - `RECONCILE_EXEMPT` (`test_schema_reconcile_coverage.py`) for a NOT NULL column with no default.
- Run the Postgres suite (`VIBERPIT_TEST_PG_URL`; see memory "Running the Postgres suite locally").

**Settings.**
- `AppSettings.repo_root` (`models.py:546`) is the pattern to copy, along with `server/api/settings.py` (`SettingsBody` `:101`, PUT `:222`, `_to_dict` `:161`, `effectiveDefaults` `:79`).
- Every daemon reads the DB itself via `effective_repo_root` (`orchestrator.py:574-592`).
- On the web it lives in `SessionDefaultsSection.tsx` and `settingsMeta.ts:53-56`.

**Backfill candidates.**
- Use `sessions.branch` only where it passes the built-in matcher **and** has the exact `<base>-<short sid>` or `fork-<short sid>-<n>` shape for that row's id.
- `sessions.branch` can hold the user's own branch (path 2), so it is not proof by itself.

## Global Constraints

**Ownership record.**
- A new model, `SessionBranch`, in `server/db/models.py`, with fields:
  - `id`
  - `session_id` (indexed; no FK cascade, because the record must outlive a hard-deleted session row while the branch exists)
  - `host`
  - `clone` (the realpath of the repo's common git dir's top)
  - `branch`
  - `source`: `create` | `fork` | `import` | `backfill`
  - `created_at` (`db_utc_now`)
- It has a unique constraint on `(host, clone, branch)`.
- Add migration `0109_session_branches`, chained off `0108_session_snooze` (renumbered from 0108 when #3349 landed first); Task 3's `AppSettings.branch_prefix` is `0110_app_settings_branch_prefix`.
- Recording rules:
  - Record when ViberPit actually created the branch: `create_worktree` with `is_child=True`, every fork, and `import_` when `prev is None` **and** the source host had a record for it. The source record travels with the move (bundle metadata or a DB read on the target, since it is one shared DB).
  - The recording must succeed before the session is reported started. If the DB write fails, the branch still exists but is unrecorded, so it is never auto-deleted (fail closed). Log at WARNING with a bare event name.
  - Never record the user's own branch (path 2) or anything that came from the `branch` request field without `-b`.

**Deletion gate.**
- `remove_worktree` gains a required keyword, `owned_branch: Callable[[str], bool]` (no default, so no caller can silently skip it), replacing the bare `is_session_branch` check.
- A branch is deleted only if:
  - `owned_branch(branch)` is True;
  - the existing unpushed-commit and snapshot guards pass.
- Server callers build the predicate from the DB for `(host, clone)`:
  - **Recorded** → owned.
  - **Unrecorded but legacy** → owned only if it matches a built-in prefix **and** has the exact name shape for the session being removed. The legacy rule is limited to the session's own branch shape, so an arbitrary `viberpit/foo` stays.
  - **Anything else** → kept. An unknown or failed DB read keeps the branch.
- The janitor resolves the records once per sweep, before entering the thread (the `live_cwds` precedent), and passes a predicate over that snapshot.
- On a successful delete, remove the record.
- `_discard_fork` and the `import_` rollback are unchanged: they delete only what they just made.
- The Discard preflight `sessionBranch` flag and `branchDeleted` use **the same predicate**, so the dialog copy matches what will actually happen. This is the single source of truth.

**Backfill.**
- A one-shot `DataBackfill` in `init_db` records `source=backfill` rows for live and archived sessions whose `branch` passes the strict legacy rule (built-in prefix plus that row's exact shape).
- It is idempotent and keyed by host and clone. It resolves the clone from the row's worktree cwd if present, otherwise via `clone_path`; if it can't be resolved, it skips the row.

**Configurable prefix.**
- Add `AppSettings.branch_prefix` (nullable; null means the built-in `viberpit/`).
- Validation on PUT, enforced server-side:
  - The prefix must end in `/`, be 2–40 characters, and pass `git check-ref-format --branch <prefix>x` (argv, no shell).
  - It may not be one of the reserved common namespaces: `feature/`, `feat/`, `fix/`, `bugfix/`, `hotfix/`, `release/`, `dependabot/`, `renovate/`, `main/`, `master/`, `pm/`, `gh-pages/`. This list is a module constant with a comment.
  - It may not equal or nest under another prefix in a way that would match existing **unrecorded** local branches in the edge host's clones under `repo_root`. Refuse if any such branch exists, and name up to three in the error.
  - The error code is `BRANCH_PREFIX_INVALID` with a reason, returned as 400.
- The name builders take the prefix as a parameter. The server resolves the effective prefix from the DB at spawn and fork time, the same way as `effective_repo_root`, and passes it into `create_worktree` and `create_fork_worktree`.
- `is_session_branch` keeps meaning "matches a built-in prefix" for legacy and display. It is never again the deletion gate alone; update its docstring.
- `_next_fork_branch` scans the effective prefix as well as the built-in prefixes.

**Web.**
- Settings › Sessions replaces the read-only "Fixed" row with an editable "Branch prefix" field:
  - placeholder `viberpit/`, plus a Reset button;
  - copy: "New session branches start with this. ViberPit only ever deletes branches it recorded creating, so changing it never makes your own branches deletable.";
  - server errors are shown inline.
- `web/src/brand.ts`'s fixed prefix stays for display defaults. `effectiveDefaults.branchPrefix` is what the UI shows.

**Gates.**
- Server:
  - ruff;
  - mypy;
  - focused pytest plus parity;
  - core `tests/` (`worktrees.py` changes): run with `PYTHONPATH=$PWD/src`;
  - **the Postgres suite** (new model);
  - the full `server/tests -n 4`.
- Web: typecheck, lint, the full vitest, build, budget, e2e.

## Review Focus

1. **No path deletes an unrecorded branch.** Exceptions are the strict legacy shape rule and the deletes of branches a call itself just created. An unknown DB read keeps the branch. (Tasks 1–2.)
2. **The user's own branch (path 2) is never recorded or deleted**, whatever the prefix. (Tasks 1–2.)
3. **The Discard dialog's "will be deleted / kept" copy uses the same predicate as the deletion.** (Task 2.)
4. **A clashing or reserved prefix is refused server-side.** Prefix validation never runs a shell. (Task 3.)
5. **Multi-host: records are host- and clone-scoped.** A record on rocky never authorises a delete on dobby. Ownership survives a move. (Tasks 1–2.)

---

### Task 1: Ownership registry — model, migration, recording, backfill

**Files:**
- `server/db/models.py`
- `server/db/migrations/versions/0109_session_branches.py`
- `server/db/__init__.py` (backfill)
- a new `server/sessions/branch_ownership.py`: record, lookup, the predicate builder, the legacy shape rule
- the orchestrator spawn, fork and move-import recording calls
- `src/viberpit/worktrees.py` (return `is_child` or the created-branch info where needed; no DB)
- the Postgres test lists

**Tests:**
- `server/tests/test_branch_ownership.py`, with a real temp git repo and an isolated DB;
- core `tests/test_worktrees*.py` for any `src` change;
- the Postgres compat lists.

Steps:
- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run focused tests, parity, ruff, mypy and core.
- [ ] Commit `feat(sessions): record the branches ViberPit creates (D-22)`.

### Task 2: Deletion gated on ownership

**Files:**
- `remove_worktree` (required `owned_branch`) and every caller: janitor, Discard, close and rollback, transfer finalize;
- the janitor sweep (resolve once per sweep);
- the preflight `sessionBranch` and `branchDeleted` fields;
- record removal on delete.

**Tests:**
- An unrecorded `viberpit/foo` is kept.
- A recorded branch is deleted.
- The legacy shape for the session's own id is deleted.
- Another session's legacy-shaped branch is kept.
- A DB failure keeps the branch.
- A rocky record doesn't authorise a delete on dobby.
- The preflight copy matches the outcome.

Steps:
- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run gates.
- [ ] Commit `feat(sessions): delete only branches ViberPit recorded creating (D-22)`.

### Task 3: Configurable prefix — setting, validation, builders, web

**Files:**
- `AppSettings.branch_prefix` plus `settings.py` (validation, `effectiveDefaults.branchPrefix`);
- the name builders, which take the prefix;
- spawn and fork resolve it;
- `_next_fork_branch`;
- web `SessionDefaultsSection.tsx`, hooks and types, plus their mocks.

**Tests:**
- Server: validation cases (format, reserved, clash with unrecorded local branches, reset).
- A spawn with a custom prefix creates and records `<prefix><base>-<short>`.
- A fork with a custom prefix.
- Web: unit tests plus e2e `web/e2e/branch-prefix.spec.ts`.

Steps:
- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run the full gates.
- [ ] Commit `feat: configurable session-branch prefix (D-22)`.

### Task 4: Benchmark and docs

- Change the benchmark row "Worktree location / policy" to Y, then regenerate §3.
- Update the spec (Worktrees row, §11 D-22), the product docs and `deploy/README.md` if ops notes are needed.
- Update the `is_session_branch` docstrings.
- Ceilings: measured + 0.3, and only if they moved.
- [ ] Commit `docs: D-22 benchmark and branch-ownership docs`.

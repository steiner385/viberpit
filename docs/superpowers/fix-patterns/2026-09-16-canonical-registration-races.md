# Canonical repository registration races

## Symptom

Two products can register different spellings of the same GitHub repository, making
policy ownership ambiguous. A check before insertion alone still lets simultaneous
requests both observe an unregistered repository and create competing owners.

## Anti-pattern

Resolving canonical identity or checking existing registrations without a database
serialization point around the final ownership check and insert. A process-local
lock does not protect independent edge processes; network lookups inside a write
transaction hold scarce locks while waiting on GitHub.

## Fix shape

Gather the existing `repo_project` ownership snapshot before the request transaction.
Acquire PostgreSQL's `SHARE ROW EXCLUSIVE` lock on `project_repos`, or SQLite's writer
reservation with `BEGIN IMMEDIATE`, as the first SQL in the request session. Recheck
registration evidence and resolve ownership under that lock, then insert and commit.
Changed evidence receives an actionable conflict and requires a fresh preflight.
Only verified missing ownership permits a new product registration; canonical aliases
within that request coalesce, preserving the requested primary repository. Existing
unique/ambiguous owners and unresolved identities refuse before any project, audit,
or repository rows are written. No uniqueness migration deletes legacy ambiguity.

The lock is intentionally global for this low-volume registration operation, and
lasts only for database work. It also serializes ordinary mirror rename writes on
PostgreSQL without requiring a second advisory-lock convention. All future writers
that introduce product ownership must follow the same registration entry point.

## Where to look

- `server/api/pm.py:create_project` is the only live creation of `ProjectRepo` rows.
- `server/pm/repo_project.py` owns canonical identity, ownership resolution and the
  registration serialization boundary; it is not a second repository registry.
- `server/pm/mirror_store.py:rename_repo` rewrites already-owned aliases and coalesces
  them within the same product. It preserves different products' legacy ownership;
  it does not introduce a new product owner and remains unchanged.
- `server/pm/sync_request.py` and `server/automations/pm_sync.py` update existing
  registrations' synchronization metadata, not ownership.
- Tests exercise two independent transactions on file-backed SQLite and an isolated
  PostgreSQL schema, synchronized after preflight, with exactly one successful owner.
  API regressions cover aliases, primary selection, legacy ambiguity, inaccessible
  evidence, null slugs, and no partially-created products on refusal.

# Resolve both sides of checkout containment

## Symptom

Deploy interpreter preflight rejected a valid service launched through a compatibility
checkout symlink: the interpreter existed and its virtual environment belonged to
that same checkout, but preflight reported an external virtual environment.

## Anti-pattern

Comparing a resolved child path against an unresolved parent treats a parent alias
as an escape. A physical checkout rename with a backward symlink exposes this even
when the virtual environment itself has never moved outside its owning checkout.

## Fix shape

Resolve the parent as well as the virtual environment before testing containment.
Keep the path separator boundary and continue rejecting virtual environments that
resolve into a different checkout. Do not resolve only the interpreter executable:
normal virtual environments link that executable to a system Python.

`server/deploy/steps.py:check_unit_interpreters` implements the fix.
`test_checkout_rename_alias_keeps_its_own_venv` failed before the fix and passed
afterward; the existing external-venv and system-interpreter tests still pass.

## Where to look

Repository-wide Python searches covered `realpath`, `relative_to`, `is_relative_to`,
`commonpath`, and path containment `startswith` calls. Inspected candidates:

- `server/codex/driver.py` attachment containment resolves both workspace/store roots
  and attachment paths; no matching defect.
- `server/library/names.py:resolve_under` and
  `server/library/snapshots.py:_safe_dest` resolve both sides; no matching defect.
- `src/ccswitch/{paths,account_names,auth_login,worktrees,configdir}.py` containment
  guards resolve both parent and child; no matching mixed-resolution comparison.
- `server/sessions/orchestrator.py` scratch cleanup and archive deletion resolve
  both sides; no matching defect. `scratch_config.py` does the same.
- `server/codex/auth_endpoint.py` key identity and `server/sessions/adopt.py`
  account identity compare resolved paths on both sides.
- `server/sessions/permissions.py` memory paths intentionally use lexical,
  cfg-anchored matching, explicitly documented to avoid approving repository writes.
- Library scanning/snapshot iteration and script relative paths derive child paths
  directly from the same parent; these produce labels, not mixed-resolution guards.

Two lexical-only consumers remain outside this cutover fix: clone-doctor repository
name derivation and Codex manager orphan selection by `CODEX_HOME`. They do not
compare a resolved child against an unresolved root, but aliases could affect
recognition if those persisted session/account namespaces were migrated. This
cutover preserves those namespaces; inspect these consumers before any future
namespace migration. No live state or additional runtime code changed in the audit.

# Refuse unknown repository ownership before PM mutations

## Symptom

Ambiguous repo registrations disappear from the compatibility owner projection.
Triage can then accept without a scoped feature lookup, while issue disposition,
dependency removal, and materialization discover ownership only after external
writes have already happened.

## Anti-pattern

Treating a missing entry in a lossy read projection as authorization to mutate,
or gathering canonical identity after taking a write/row lock.

## Fix shape

Gather verified GitHub repository aliases before the mutation transaction using
the same canonical identity transport as implementation admission. Carry both
identities and the exact repository/project-id/slug registration evidence into
the shared service. Re-read that evidence and run the canonical owner resolver
before any overlay, journal, or external mutation. Refuse ambiguous, unresolved,
changed, or mismatched evidence. Omitted preflight also refuses direct callers.
Bulk accept shares a preflight; automatic and human accepts use the same guard.
Expected ownership conflicts are HTTP 409 (or per-item bulk refusals).

Verified missing ownership remains explicitly supported for human issue and
dependency decisions and manual triage. Existing standalone materialization is
supported only with no explicit Initiative.project_id, matching its documented
nullable product/membership behavior. Automatic triage requires a unique owner;
an explicit initiative product must match it. None of these exceptions grants
fleet-level automation authority.

## Where to look

The sibling sweep covered `triage_accept`, `issue_disposition`, `dep_remove`, and
`materialize_apply`; their write guards share `pm.repo_project`. Drift sibling
reproposal and post-write sync use canonical preflight ownership rather than a
second registration-only lookup. Implementation admission shares identity
snapshot gathering without changing dispatch policy.

The PM project-registration endpoint still needs a separately designed canonical
duplicate-registration guard with concurrent insert semantics. A check-then-insert
alone would not solve that race. This change therefore closes the bounded mutation
preflight gap, not all A3 or Phase 3 acceptance criteria. Revalidation detects
registration changes before the mutation transaction; it does not claim an atomic
transaction spanning GitHub and all subsequent registration edits.

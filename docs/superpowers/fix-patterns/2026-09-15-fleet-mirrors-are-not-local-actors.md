# Fleet mirrors are not local recovery actors

## Symptom

A primary daemon attempts `ensure_driver` on a session leased to a worker, producing
`session.lease_denied reason=resume`. An autopilot nudge then drops its prompt because
no local driver exists (#1738). The lease guard prevents duplicate execution; the
caller is choosing an actor from records loaded for fleet visibility.

## Anti-pattern

Treating all entries in the primary's `_records` as locally actionable, or querying
shared pending-rearm rows without ownership filtering. Ignoring an `ensure_driver`
None result compounds the failure by attempting an action after lease refusal.

## Fix shape

Use `_enabled_host_names` and canonical `_owns_record` for autonomous local actors.
Filter autopilot selection before local context inference and recheck at apply after
the decider await. Stop nudge, permission and question actions if driver acquisition
returns None. Filter startup auto-resume and background-rearm scans with the same
policy; leave foreign pending snapshots untouched for their owner.

Keep lease acquisition authoritative. Local/live-driver ownership, legacy primary
rows and orphan adoption retain their existing policy; an unknown host registry
must not authorize foreign recovery. Explicit per-session routing/adoption paths
(`ensure_driver`, `resume_interrupted`, `_rearm_session` on attach) remain unchanged.

## Where to look

- Fixed: `_autopilot_list_governed`, `_autopilot_apply`, `auto_resume_interrupted`,
  `rearm_background_tasks` in `server/sessions/orchestrator.py`.
- Already guarded: `_recover_undelivered_prompts`, rotation/stuck sweeps, titling,
  inferred-goal sweeps; `_deliver_initial_prompt` and completion corrective nudges
  check the driver result before sending.
- Fixed later (#1824, 2026-09-16): `restore_deploy` and `_recover_resume_failures_once`.
  The deploy snapshot lists the drivers this process *held*, not the rows it *owns*,
  so a foreign row grabbed once (by the pre-#1738 autopilot path) was re-resumed by
  the primary on every deploy restart — in a bare `mkdir`'d copy of the worktree
  path — and its lease fenced the owning worker out of its own session for a day.
  Both paths now apply the same `_owns_record` fence: skip (logged as
  `deploy.restore.skip_foreign` / `deploy.resume.skip_foreign`), never enroll for
  retry. Rows with no in-memory record, NULL-host rows and orphans keep their
  existing policy. Explicit attach rearm goes through per-session routing.
- Limitation: autopilot runs on the primary and assembles context from local driver,
  permission and question registries. Remote-worker governance needs routed context
  AND actions; skipping foreign mirrors fails closed instead of pretending local
  seams support it. No new RPC surface is added by this fix.

The warning alone does not identify its caller. The observed denial followed by a
prompt drop matches autopilot's unchecked driver result; tests reproduce that path
and the independently demonstrated startup scan defects without claiming a trace
that the historical journal does not contain.

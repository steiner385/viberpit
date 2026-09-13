# Unknown observations and mismatched workload populations

Refs #1611, #1550.

## Symptom

An unavailable automation observer appears idle, a failed count read appears as zero,
or the fleet workload total cannot be reconciled with the product rows.

## Anti-pattern

Default configuration or an empty fallback is presented as measured runtime state.
Separately sampled totals and breakdowns count different populations: the global
claim count includes historical productless work and work for disabled products,
while the product table includes only products eligible to dispatch new work.

## Fix shape

Keep unknown measurements explicit. Isolate failures in a batched observation so
one unavailable producer does not blank every row. Share the attention count
between navigation and list rows; do not substitute active sessions for actionable
items. Preserve the difference between a scheduled wait and an execution failure.
An observed human decision keeps attention even when the service is disabled or
scheduled off. Zero live sessions and zero automation action buttons do not mean
a product-level approval was resolved.

Derive displayed workload totals and breakdowns from one claim snapshot through
the existing in-flight predicate. Include active work for disabled products and
report unattributed work explicitly. Do not infer or rewrite historical ownership
to make the arithmetic match. A proposed claim occupies a slot but is not evidence
that an implementation session is running.

## Where to look

- `server/api/automations.py`: unavailable daemon responses and list count payload.
- `server/sessions/orchestrator.py`: status context wiring, per-provider isolation,
  backlog query/count separation, and retained observations after refresh failures.
- `server/automations/backlog_runner.py`: claim snapshot, product filtering, and
  exception-to-zero fallbacks.
- `web/src/features/delivery/`: attention tiers and missing-count presentation.
- `web/src/shell/useNavBadges.ts`: count semantics shared with the rows.

The automation API and renderer's null-context idle fallbacks were replaced in the
Phase 1 work. The runner regression tests cover failed measurements, off-product
work, and productless claims. Retained producer counters need explicit observation
freshness; a fresh HTTP response alone does not prove the underlying measurements
are fresh.

The producer sweep found three related failure paths: PM Sync reset a failed
Projects observation when that pass was skipped on cadence; GitHub Reconcile
published successful zero workload counts from guard branches before doing the
workload read; and the intake adapter swallowed a failed queue read as an empty
queue. Keep independently refreshed observations' failure state until their own
read recovers, distinguish guard observations from workload measurements, and
propagate failed reads to the loop's observation boundary. A successful empty read
is zero; a read that did not happen or failed is not.

PM onboarding's remote default view is a separate candidate to examine when
extending product-level effective state.

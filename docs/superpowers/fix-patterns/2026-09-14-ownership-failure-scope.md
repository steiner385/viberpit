# Ownership reconciliation failure scope

## Symptom

A deleted or inaccessible repository, or malformed legacy work reference, prevents
new automation work in every healthy product.

## Anti-pattern

Scanning all legacy actors and propagating every identity failure before deciding
whether its scope can conflict turns a per-target guard into a fleet-wide outage.

## Fix shape

Continue to resolve aliases using the canonical target resolver. Successful node
identity evidence always overrides product metadata. For unresolved/malformed
records, contain refusal using the existing PM product/repository bindings and
session/claim provenance. Unsynced repository spellings, matching explicit refs,
and unknown scope cannot establish non-overlap. Preserve failures for relevant or
unknown work; never use a blanket catch-and-continue around identity resolution.

PM sync canonicalizes ProjectRepo through `mirror_store.rename_repo`; a completed
sync watermark distinguishes these bindings from newly entered repo spellings.
This is scope evidence, not a substitute for canonical target acquisition. There
is no retained repository alias/node-ID authority today; a later rename can make
persisted scope stale, which needs consideration if stronger identity evidence is
required for migration policy.

## Where to look

`ownership_reconciliation.reconcile_legacy_targets`, especially malformed batch
and consolidation handling as well as resolver errors. Sibling search of all
`resolve_targets` callers found `AutomationContext.claim_targets`, which resolves
only the requested batch and must continue to fail the whole batch on any error;
it does not scan unrelated products and needs no equivalent catch.

Regression tests distinguish same-product, unrelated-product, unknown-scope,
malformed batch/consolidation and verified-alias overlap.

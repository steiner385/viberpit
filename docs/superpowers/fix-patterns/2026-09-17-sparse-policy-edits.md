# Preserve explicit policy choices during partial edits

## Symptom

Changing one product setting persisted every schema default and replaced unrelated choices. Two clients could overwrite each other. Clearing a policy removed the edit timestamp, making an explicit reset indistinguishable from a product never edited; legacy seeding could opt it back in.

## Anti-pattern

Treating effective/default-filled configuration as stored intent, then writing a whole JSON object without checking the revision read. A pre-read check alone cannot prevent two transactions from both passing it.

## Fix shape

`server/automations/product_policy.py` owns the sparse validated merge and atomic revision-checked write, reusing `PRODUCT_POLICY_SCHEMA` and `validate_config`. Validate existing and incoming choices independently; invalid stored state refuses a partial edit. Explicit null remains a safe recovery/reset. Keep effective defaults in readers, without changing the implementation decision's effective `policyRevision` hash.

`PATCH /api/pm/projects/{slug}/policy` requires `policy`. A dictionary merges only supplied keys; `null` deliberately resets. `expectedUpdatedAt` optionally carries the exact `automationPolicyUpdatedAt` returned on the project. An explicit null means the caller observed no prior edit. Missing expected revision supports compatibility callers, but the server still atomically compares against its own observed revision. Conflicts return 409 without mutation/audit; clients refresh and ask the operator to try again, never retry automatically.

Each successful write, including reset or an empty patch, advances the existing timestamp strictly (at least one microsecond). UTC serialization retains microseconds; clients must forward the string without rounding via JavaScript Date. SQL compares the existing revision and null policy evidence where applicable. The writer uses UPDATE RETURNING and refreshes the ORM object so a later flush cannot overwrite it with stale state.

Legacy runner migration uses the same writer, skips any explicit policy or edit timestamp, and records counts/audit only for successful writes. It persists only the fields present in legacy configuration; effective defaults remain the same. Future permission migrations must respect both sparse intent and a reset's revision.

The editor sends one changed key and its observed revision, disables controls during saves and conflict refresh, and installs the returned merged project. No new policy permissions, database columns or default grants are introduced.

## Where to look / sibling audit

A repository search for `automation_policy` assignments found two production writers: the PM PATCH handler and Backlog Runner legacy seeding in `server/sessions/orchestrator.py`. Both now use the canonical writer. Test seed assignments intentionally remain direct fixtures. Search frontend `updateProductPolicy` and `useUpdateProductPolicy` consumers: the automation policy card is the sole live editor; its whole-effective-object write was removed. Other automation config endpoints are distinct schemas/contracts, outside this product-policy prerequisite.

SQLite and isolated-schema PostgreSQL tests synchronize two separate sessions after their reads, proving one CAS winner with both null and existing revisions. Frozen-clock tests verify strictly increasing microsecond revisions. API tests cover missing/null, sparse compatibility edits, stale reset refusal, corrupt-state refusal/recovery and audit effects; migration tests cover prior resets and stale snapshots. Frontend tests verify exact request shape, disabled pending fields and conflict refresh without retry.

Rollout boundary: all API and daemon policy writers must run this change before relying on revision guarantees fleet-wide. Older binaries still perform unconditional replacement and cannot honor this CAS contract. No cross-version guarantee or responsibility-permission rollout is claimed by this prerequisite.

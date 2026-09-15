# Reserved generation zero and first-start policy

## Symptom
A policy preview refuses automatic implementation, but the first native driver still opens. Found while verifying Phase 3 policy admission (Refs #1611).

## Anti-pattern
A test fabricates a missing execution generation instead of using the real reservation transition. The policy guard recognizes `None`, while `reserve_session()` durably records generation `0` for work that has not entered native execution.

## Fix shape
Recognize both unset and reserved-zero generations at the first-start policy boundary. Build regression fixtures through `reserve_session()` and `begin_session_execution()` so tests distinguish reserved work from existing work. Preserve continuation after a positive native generation.

## Where to look
Inspect first-entry consumers of ownership snapshots whenever reservation representation changes. The sibling sweep covered generation checks in ownership, orchestration, and native fencing. Other missing-generation checks validate stopped proofs and must not treat a reservation as proof of completed work; no sibling change was required.

Verified with a failing real-reservation regression and an isolated supported-native smoke: policy off refuses first start before a driver opens.

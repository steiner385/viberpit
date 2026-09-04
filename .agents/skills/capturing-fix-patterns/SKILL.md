---
name: capturing-fix-patterns
description: Audit for sibling instances immediately after verifying a bug, test failure, or unexpected-behavior fix whose underlying mistake could plausibly recur elsewhere.
---

# Capture Fix Patterns

Trigger when the thought is “this could exist elsewhere,” not only when another
instance is already known.

1. Verify the original fix first.
2. Describe the reusable bug shape in one sentence without tying it to one file.
3. Search the full repository for sibling instances of the anti-pattern and inspect
   each candidate before changing it.
4. Fix in-scope siblings when the correction is low-risk and follows the same source
   of truth. Report larger or uncertain candidates separately.
5. Capture durable findings with `Symptom`, `Anti-pattern`, `Fix shape`, and
   `Where to look` when the project has an established location for fix-pattern notes.
6. Create a tracking issue only when the user requested issue management or already
   authorized that external mutation. Include the failure mode, known instances,
   likely locations, and appropriate technical-debt labels.

Use `eliminating-parallel-implementations` when the sibling shape is duplicated logic
or configuration that should share one source of truth.

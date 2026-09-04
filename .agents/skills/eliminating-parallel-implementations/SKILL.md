---
name: eliminating-parallel-implementations
description: Find and consolidate multiple implementations of one concept when surfaces disagree, a fix fails to propagate, or a new constant, helper, registry, config list, schema, or token may duplicate an existing source of truth.
---

# Eliminate Parallel Implementations

The defect is the drift between copies. Consolidation is the fix when independent
copies represent one concept and divergence would be a bug.

1. Name the shared concept precisely.
2. Search the whole repository with `rg` for the value, keys, synonyms, and decision
   shape. Include tests, configuration, server code, frontend code, and styles.
3. Classify each hit. Do not consolidate coincidentally equal values or intentionally
   different policy. Two occurrences may remain local when coupling would be worse;
   a third independently maintained copy is a strong extraction signal.
4. Choose the boundary where the truth belongs: canonical module, typed registry,
   schema, design token, or configuration source.
5. Route every real consumer through that source and delete the old copies in the
   same change. Prefer types or generated derivations where they prevent future drift.
6. Re-run the repository search and relevant tests to prove no live copy remains.

Stop before adding a new parallel helper or constant merely to keep a patch small.

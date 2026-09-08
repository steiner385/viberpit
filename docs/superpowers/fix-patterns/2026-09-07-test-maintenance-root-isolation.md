# Isolate every filesystem maintenance root in tests

**Symptom:** A shutdown test exceeded its unchanged wall-clock limit while a startup
janitor scanned the operator's real worktree tree. Its captured sweep reported zero
removals, but the janitor is capable of deleting eligible trees.

**Anti-pattern:** Assuming that a temporary account store and database also isolate
independently configured repository/worktree roots. A test's empty session registry
cannot identify and protect real live sessions' worktrees.

**Fix shape:** Extend the existing pre-collection state-isolation guard to redirect
both repository-root environment prefixes to temporary storage. Preserve explicit
temporary harness roots and reject paths that only resemble the temporary prefix.
Do not weaken the shutdown assertion or disable janitor behavior to hide the delay.

**Where to look:** Config defaults for maintenance roots, application startup tasks,
and fixtures that start real orchestrators. The sibling audit found that default-root
startup fixtures share the corrected guard; explicit-root fixtures use temporary
paths or synthetic settings/API-only paths.

The isolated fixture and shutdown checks passed together with the original timing
limit. Application runtime code is unchanged by this test-harness correction.

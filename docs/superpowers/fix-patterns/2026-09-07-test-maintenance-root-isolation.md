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

## Shared settings and generated files also affect verification

A temporary account store does not isolate `paths.shared_base()`: without an
external temporary `CLAUDE_CONFIG_DIR`, default-model lookup can read the host's
shared Claude settings. A gated host default then parks fake test sessions before
their drivers start. For local suite runs, use an empty temporary shared config
outside the account store and unset inherited `ANTHROPIC_MODEL`; do not alter the
host's settings or weaken the approval gate. Tests of explicit model policy still
set their own inputs. Three model/driver tests that failed against the host default
passed with only this process-local isolation.

Run backend gates with source files frozen. `inspect.getsource()` combines loaded
code line numbers with files on disk, so edits during a run can produce false guard
failures. Keep generated `web/dist` outside the checkout during backend tests, as
in the backend CI job: the SPA fallback otherwise changes unmatched API requests
from 404 to 405. Preserve the build for restoration instead of deleting it. In the
automation oversight verification, all nine failures passed after these isolation
corrections without product-code changes.

A completion unit fixture can also reach the shared transcript tree through
`_session_is_parked`, even when its question and repository-observation gates are
mocked. Fixtures with no transcript or background work should make that absence
explicit at the parked-session seam; parked-state cases can override it. In
#2687, this preserved the existing two-second observation deadline and the
new-turn concurrency assertion. The sibling audit found that the parked-session
case already overrides that seam explicitly.

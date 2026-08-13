# Manual archives are exempt from auto-unarchive

**Date:** 2026-08-13
**Status:** Approved (design)

## Problem

When a user **manually archives** a session, that is a deliberate decision: "I'm done
with this / I don't want this work continued." Today the **Archive Audit** automation
(`server/automations/archive_audit.py`) can silently override that decision.

Every N days the audit re-judges *recently archived* sessions against the completion bar
(`completion_bar.met_bar` — the same predicate live autopilot uses) and **auto-unarchives**
any it buckets as `"premature"` (no merged/armed PR). Its candidate query
(`list_recent_archived_rows`, and the CLI's `load_archived_rows`) selects **every**
`status == "archived"` row and ignores `archived_by`. So a manually-archived session with
no merged PR is treated identically to a possibly-wrong autopilot archive, and gets
resurrected against the user's intent.

This is the missing mirror of an existing pattern: a manual **unarchive** already opts a
session out of future auto-**archive** (`auto_archive_opt_out_reason="manual-unarchive"`,
honored at `orchestrator.py:1341`). Manual **archive** has no matching opt-out from
auto-**unarchive**. This design adds it.

## Decision

The Archive Audit may only auto-unarchive sessions the **system** auto-archived. A session
is auto-unarchive-eligible **iff** `archived_by ∈ {"autopilot", "backlog-runner"}`.
Manual (`"user"`) and unknown/legacy (`NULL`) provenance are never candidates — the audit
only ever second-guesses decisions the system provably made. (User-selected scope:
"only known auto-archives".)

## Source of truth

The provenance set `{"autopilot", "backlog-runner"}` already exists implicitly at
`orchestrator.py:6180` (the unarchive path uses it to decide whether to record an
`archive_reversed` learning heuristic). To avoid two copies of one concept drifting apart,
introduce a single canonical constant and route both call sites through it:

```python
# server/db/models.py, beside INACTIVE_SESSION_STATUSES
# Values written to Session.archived_by by an AUTOMATIC archive (vs a manual "user"
# archive). Only these are eligible for auto-unarchive by the Archive Audit; a human's
# manual archive is a deliberate decision the audit never second-guesses.
AUTO_ARCHIVE_PROVENANCE: frozenset[str] = frozenset({"autopilot", "backlog-runner"})
```

## Changes

1. **`server/db/models.py`** — add `AUTO_ARCHIVE_PROVENANCE` constant (above).

2. **`server/automations/archive_audit.py`** — add the provenance predicate to both
   candidate queries:
   - `list_recent_archived_rows` (the live loop's `list_rows` source):
     `... .where(status == "archived", archived_at IS NOT NULL, archived_at >= since,
     archived_by.in_(AUTO_ARCHIVE_PROVENANCE))`
   - `load_archived_rows` (the `archive-audit` CLI): add
     `archived_by.in_(AUTO_ARCHIVE_PROVENANCE)` to its `where`.

   Filtering at the query keeps the audit's `total`/counts honest — manual archives are
   simply not auditable candidates, not "audited then skipped".

3. **`server/sessions/orchestrator.py:6180`** — replace the inline
   `("autopilot", "backlog-runner")` tuple with `AUTO_ARCHIVE_PROVENANCE` (import from
   `server.db.models`, which is already imported here). No behavior change; dedupe only.

Nothing else auto-unarchives (verified: `restore_deploy` only re-launches sessions that
had a *live driver* at snapshot time — archived sessions have none; there is no other
resurrection path).

## Testing

`server/tests/` (archive-audit unit tests exercise `run_audit`/`list_recent_archived_rows`
with injected rows — no network):

- **Regression (the fix):** a `"user"`-archived, PR-less (would-be "premature") row is
  **not** returned by `list_recent_archived_rows` / `load_archived_rows`, so the loop
  never unarchives it.
- **Preserved behavior:** an `"autopilot"`-archived premature row is still returned and
  still unarchived.
- **Legacy/NULL:** a `NULL`-`archived_by` row is excluded (already excluded in the recency
  loop via `archived_at IS NOT NULL`; assert it for `load_archived_rows` too).
- **Dedupe:** existing unarchive-reversal tests still pass with the constant swap at 6180.

## Out of scope

- No UI change — this is a backend correctness fix.
- No new column/migration — provenance is already recorded; we only start honoring it.
- No change to the manual-unarchive → no-auto-archive direction (already works).

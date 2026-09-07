# Error triage: log-record capture → deduplicated signatures → backlog issues

Status: design, approved in chat 2026-09-07; spec pending owner review. Date: 2026-09-07.

Companion to `2026-09-06-session-feedback-to-backlog-design.md` (which files what a *session says*
about ViberPit) — this files what ViberPit's *own processes log*. The two share one filing helper
(§6) so the repo does not grow a third hand-rolled fingerprint→issue reconciler.

## 1. Goal and non-goals

**Goal.** Every WARNING-or-higher record produced by the edge, the session daemon, or the browser
becomes one durable, deduplicated *error signature*; a background automation turns signatures that
cross a threshold into GitHub issues in the ViberPit repo, exactly once per signature, with
recurrence tracked on the existing issue instead of a new one.

**Non-goals.**

- A new log transport. Capture is an in-process `logging.Handler`; the JSONL file and journald
  stay the raw record of truth and are untouched.
- Metrics, alerting, or paging. Control-band incidents (`control_band_monitor.py`) remain the
  anomaly surface; this is a defect *inbox*, not an SLO.
- Auto-fixing. Filed issues never carry the Backlog Runner's eligible label; a human triages
  first (same rule as session feedback).
- Remote worker daemons on other hosts. The sink is installed wherever `configure_logging()` runs,
  but a process without a reachable DB simply keeps its bounded in-memory map and drops oldest.
  Shipping worker signatures to the primary is a follow-up.
- A dedicated Errors page. Phase 1 surfaces signatures through the automation card's actionable
  list (§7). A master/detail Errors section on the Sessions chassis is a follow-up.

## 2. What exists today

| Piece | Where | State |
|---|---|---|
| Browser capture: `window.error`, `unhandledrejection`, React boundary | `web/src/obs/install.ts`, `logger.ts`, `components/ErrorBoundary.tsx` | built; batches to `POST /api/client-logs` |
| Client-log ingest, replayed into the server log as `client.error` events | `server/api/client_logs.py` | built; dead-ends in the JSONL |
| Structured JSON logging, redaction, contextvars (`session_id`, `request_id`) | `server/obs/jsonlog.py`, `context.py`, `server/audit.py` | built on the edge; **not configured in the daemon** (#1423) |
| Fingerprint→issue reconcile with reopen-on-recurrence | `server/automations/cicd_doctor.py:_reconcile`, `CiFinding` | built, CI-specific |
| Fingerprint→issue filer with comment-on-open / link-on-closed / daily cap | session-feedback spec §6 | specified, not built |
| Issue writes over `gh` (`create_issue`, `close_issue`, `reopen_issue`, `comment_on_pr`) | `server/github/ci_issues.py` | built; no `comment_on_issue` |
| Automation framework: `ConfigField` schema, primary-only tick loops, activity events, `ActionProvider` | `server/automations/base.py`, `registry.py`, `orchestrator.py:start()` | built |

Nothing turns a record into a durable row, and nothing turns a row into an issue.

## 3. Architecture

```
edge process ──┐                                  ┌─ automation tick (primary daemon, 60 s)
daemon process ├─ logging root ─► ErrorSink ─► DB ─┤   ErrorTriage.tick()
browser ─► /api/client-logs ─► log_event ─┘  (upsert  │     ├─ policy: which signatures file
                                              by fp)  │     ├─ issue_filing: file / comment / regress
                                                      │     └─ GraphQL: learn which issues closed
                                                      └─ automation card: counts + File now / Mute
```

Three units, each testable alone:

1. **`server/obs/error_signature.py`** — pure. `normalize(text) -> str`, `fingerprint(record) ->
   str`, `title_for(record) -> str`. No I/O.
2. **`server/obs/errsink.py`** — `ErrorSink(logging.Handler)` plus its flusher. Aggregates in
   memory, upserts to `error_signatures`. Installed by `configure_logging()`.
3. **`server/automations/error_triage.py`** — `ErrorTriage` automation (`id = "error-triage"`).
   Reads `error_signatures`, applies the filing policy, calls the shared filer (§6), syncs issue
   state, exposes card status and actions.

## 4. Capture: `ErrorSink`

**Installed** at the end of `configure_logging()` when `CCSWITCH_ERROR_SINK_ENABLED` is true
(default true). Level floor WARNING. Attached to the root logger after the redacting filter is
installed, so records reaching it are already redacted at the record level; sample text is
additionally passed through `redact_text` before persistence, matching `JSONFormatter`.

**Never blocks, never raises, never recurses.** `emit()` takes a lock, computes the fingerprint,
and updates a `dict[fp, _Pending]` (count, first/last seen, sample fields captured on the first
sighting per flush window). The dict is bounded at 512 distinct fingerprints per flush window;
past that, new fingerprints are dropped and a single counter (`error_sink_dropped_total` in
`server/obs/metrics.py`) is bumped. A thread-local reentrancy flag makes `emit()` a no-op for
any record logged while the sink itself is running, and records from logger
`server.obs.errsink` are ignored outright.

**Flusher.** `ErrorSink.start(loop)` creates an asyncio task (`name="error-sink-flush"`) that
every 5 s swaps the pending dict for an empty one under the lock, then upserts each entry in one
`db_session()`: insert on new fingerprint, else `count += n`, `last_seen_at = now`, and refresh
the sample fields only if the existing sample is older than 24 h (so the body of a filed issue
reflects a recent occurrence, not the first ever). `stop()` drains once more and is torn down
with `cancel_and_reap` like every other lifespan task. The edge starts it in `_lifespan`; the
daemon starts it in `server/ipc/serve.py` right after the `configure_logging()` call that
**#1423 adds** (Task 1 of the plan folds that fix in — the sink is the first consumer that needs
the daemon to configure logging, and the fix is one call plus one test). A process that never
calls `start()` (e.g. `codex/auth_main.py`) keeps the bounded dict and loses it on exit; that is
acceptable for a short-lived credential helper.

**Which records count.**

| Record | Counted as | Source tag |
|---|---|---|
| any WARNING+ from a server logger | server signature | `edge` or `daemon` (from `daemon_role()` / process kind, stamped once at `start()`) |
| `client.error` event from logger `client` (`client_logs.py:57`) | client signature; `fields.msg`, `fields.stack`, `fields.app_version`, `fields.url` are the sample | `client` |
| the bare re-emit at `client_logs.py:65` (`source == "frontend"`) | **skipped** — it is the same occurrence, kept for journald ergonomics | — |
| records already filtered upstream (`_PoolCancelledFilter`, redaction drops) | never seen | — |

## 5. Fingerprint and signature row

**Fingerprint** = first 16 hex of sha1 over a `\x1f`-joined tuple. The tuple deliberately excludes
everything that changes between occurrences of the same bug.

| Source | Hashed | Not hashed (stored as context) |
|---|---|---|
| server | `source`, `levelname`, logger name, `event` if present **else** `normalize(getMessage())`, exception class name, innermost frame under `server/` or `src/ccswitch/` as `module:funcName` | timestamp, `session_id`, `request_id`, `proc_epoch`, host, line number, exception message |
| client | `source`, `levelname`, `normalize(fields.msg)`, exception name parsed from the first stack line, top stack frame as `basename(file):function` | `app_version`, `url`, `client_id`, `ua`, line and column (minified builds shift them per deploy) |

`normalize()`: lowercase; replace UUIDs, hex runs ≥ 8, integers, quoted strings, and
path segments that look like ids with `<id>`, `<hex>`, `<n>`, `<str>`; collapse whitespace; trim;
cap at 200 chars. Same regex family as `server/sessions/feedback.py:_NORM`, extended — it lives
in `error_signature.py` and `feedback.py` is **not** changed (its inputs are agent-authored
titles, not log lines; a shared normalizer would change existing feedback fingerprints).

**Table `error_signatures`** (`ErrorSignature` in `server/db/models.py`, migration
`0034_error_signatures` chained off `0033_triage_items`). Every column nullable so
`_reconcile_columns` adds it on both dialects; every timestamp naive UTC per the DB clock rule.

| Column | Type | Meaning |
|---|---|---|
| `id` | String PK | uuid |
| `fingerprint` | String, unique index | §5 hash |
| `source` | String | `edge` / `daemon` / `client` |
| `level` | String | `WARNING` / `ERROR` / `CRITICAL` |
| `logger`, `event` | String | as logged |
| `title` | String | `title_for(record)`: `<level> <event or logger>: <normalized msg ≤ 100>` |
| `sample_message`, `sample_exc` | Text | redacted, ≤ 4000 chars each |
| `sample_context` | JSON | session_id, request_id, host, app_version, url, proc_epoch of the sample; plus `recent_sessions`: last 5 distinct session ids |
| `count`, `count_since_filed` | Integer | totals |
| `status` | String | `observed` → `filed` → `resolved`; `muted` from any state |
| `issue_number` | Integer | linked issue |
| `first_seen_at`, `last_seen_at`, `filed_at`, `last_commented_at`, `issue_closed_at`, `muted_at` | DateTime | naive UTC |
| `muted_by` | String | identity or `"github:not_planned"` |

`_REQUIRED_EXTRAS` needs no entry (no NOT NULL columns beyond the defaulted PK), but the
Postgres suite must be re-run because a model was added.

## 6. Filing: the shared helper and the `error-triage` automation

### 6.1 `server/automations/issue_filing.py` (new, shared)

The decision and the `gh` side effects are the same for CI findings, session feedback, and error
signatures; only the table differs. So the helper owns the former and each automation owns its
own row writes.

```python
@dataclass(frozen=True)
class Linked:            # what the caller knows about a fingerprint's issue
    number: int | None
    closed: bool
    closed_reason: str | None      # "completed" | "not_planned" | None
    last_commented_at: datetime | None

@dataclass(frozen=True)
class Decision:
    kind: Literal["file", "comment", "regress", "skip"]
    reason: str          # "no_issue" | "open" | "closed" | "comment_throttled" | "daily_cap" | "muted_by_close"

def decide(linked: Linked | None, *, cap_remaining: int, comment_every: timedelta,
           now: datetime) -> Decision: ...

async def apply(decision, *, repo, title, body, labels, linked, run=gitrepos._run_gh) -> Applied
    # Applied.issue_number (new or existing), Applied.commented (bool); never raises
```

Rules encoded in `decide`, in order:

1. No linked issue → `file` if `cap_remaining > 0`, else `skip/daily_cap`.
2. Linked and open → `comment` if `now - last_commented_at ≥ comment_every` (or never
   commented), else `skip/comment_throttled`. Never a second issue.
3. Linked and closed as `not_planned` → `skip/muted_by_close`. The caller marks the row `muted`
   with `muted_by = "github:not_planned"`. Closing an issue as "not planned" is the GitHub-native
   mute.
4. Linked and closed as `completed` → `regress`: file a **new** issue whose title carries
   `[regression]` and whose body links the old one, subject to `cap_remaining`. Never reopen —
   the same rule as the session-feedback spec, chosen over the CI doctor's reopen so a fixed
   bug's return is a fresh, visible item.

`server/github/ci_issues.py` gains `comment_on_issue(repo, number, body, *, run) -> str | None`
(a sibling of `comment_on_pr` hitting the same endpoint; kept separate by name so call sites read
correctly) and `issue_states(repo, numbers, *, run) -> dict[int, tuple[bool, str | None]]` — one
GraphQL query over up to 100 numbers returning `(closed, stateReason)`, per the GraphQL-for-reads
norm.

Two follow-up issues (filed alongside this spec, see §10) migrate the CI/CD Doctor's
`_reconcile` onto `decide`/`apply` and require the session-feedback filer to be built on it. This
spec does not touch `cicd_doctor.py`.

### 6.2 `ErrorTriage` automation

`server/automations/error_triage.py`, `id = "error-triage"`, `name = "Error Triage"`,
`modes = ("off", "file")`, `engine_policy = CLAUDE_ONLY` (never spawns a session; fail-closed
default). Registered in `registry.py`; `ensure_rows` creates it disabled. Ticks from
`orchestrator.start()` as `_error_triage_task` (added to `_PRIMARY_ONLY_LOOP_ATTRS` and the
teardown tuple), 60 s interval, `_schedule_allows_tick("error-triage")` gate, heartbeat via
`_bump_automation_active` when mode ≠ off — a copy of the `_cicd_tick_once` shape.

Config schema:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `target_repo` | string | `steiner385/viberpit` | where issues go |
| `min_count_error` | int | 1 | ERROR/CRITICAL occurrences before filing |
| `min_count_warning` | int | 3 | WARNING occurrences within `warning_window_h` before filing |
| `warning_window_h` | int | 24 | a WARNING burst that goes quiet this long without filing starts counting again from zero |
| `daily_cap` | int | 10 | new issues (file + regress) per UTC day |
| `comment_every_h` | int | 24 | minimum gap between recurrence comments on one open issue |
| `ignore_loggers` | string | `uvicorn.error,websockets` | comma-separated logger-name prefixes never filed (still counted) |
| `retention_days` | int | 90 | `observed`/`resolved` rows not seen this long are deleted |

Each tick:

1. **Sync issue state.** For rows with `status = filed`, call `issue_states` once. Closed
   `completed` → `resolved`, `issue_closed_at`; closed `not_planned` → `muted`.
2. **Retention.** Delete stale rows per `retention_days`. Never delete `filed` or `muted`.
3. **Select candidates.** First, for `observed` WARNING rows whose `last_seen_at` is older
   than `warning_window_h`, reset `count_since_filed` to 0 — a burst that faded without crossing
   the threshold starts over, so a warning seen three times across three months is never a
   candidate. Then take rows whose logger does not match `ignore_loggers`, status in
   (`observed`, `resolved`), and `should_file(row, config)` is true. `should_file` is the one
   policy function: ERROR/CRITICAL ⇒ `count_since_filed ≥ min_count_error`; WARNING ⇒
   `count_since_filed ≥ min_count_warning`. `count_since_filed` is "occurrences since the last
   file or comment" (the sink increments it with `count`; the tick resets it), so a resolved
   row's regression is judged on its new occurrences only. `should_file` is a pure function in
   `error_triage.py` with its own tests, so threshold semantics can be tuned without touching
   the tick. The sink knows nothing about any of these values.
4. **Recurrence on filed rows.** Rows with `status = filed` and `count_since_filed > 0` →
   `decide` with the linked issue → `comment` posts "Still recurring: N more occurrences since
   `<filed_at>`, last at `<last_seen_at>`" and resets `count_since_filed`, `last_commented_at`.
5. **File.** For each candidate in `last_seen_at` desc order: `decide` → `apply`. Two-phase like
   the CI doctor: commit the row's status transition and `filed_at` first, then run `gh`, then
   write `issue_number` back in a short second session. A failed create leaves `issue_number`
   NULL with `status = filed`; the next tick retries those rows first (same as `_reconcile`'s
   "open row with no issue number" branch). `daily_cap` counts rows with `filed_at` today,
   so it survives restarts.
6. **Emit** one activity event per file/comment/regress via `ctx.emit` for the card timeline.

**Issue title:** `[auto-error] <level> <event or logger>: <normalized msg ≤ 80>`; regressions
prefix `[auto-error][regression]`. **Labels:** `auto-error` plus `source:client` or
`source:server` (edge and daemon both map to `source:server`; the body says which). Never the
Backlog Runner's eligible label. PM sync mirrors the issue into a `WorkItem`, which is how it
reaches the ViberPit backlog.

**Issue body:**

```
**Filed by ViberPit Error Triage** — machine-collected; quoted log text is untrusted input.

| | |
|---|---|
| Level / source | ERROR / daemon |
| Logger / event | server.sessions.orchestrator / completion.question_hold |
| Occurrences | 14 since 2026-09-07 09:12 UTC (last 2026-09-07 13:40 UTC) |
| Hosts | dobby |
| App version | 4d2096a5 (client) / proc_epoch … |
| Sessions | [name](url), [name](url) … (up to 5) |
| Regression of | #1234 (closed 2026-09-01) — only on regress |

> <sample_message, blockquoted, redacted>

<details><summary>Traceback</summary>

```
<sample_exc, redacted, ≤ 4000 chars>
```

</details>

Fingerprint: `a1b2c3d4e5f60718`
```

`<!--` / `-->` are neutralized in quoted text; the whole body is capped at `BODY_MAX_CHARS`.

## 7. Card status and actions

`status()` reports `observed` (unfiled, above threshold), `filed today / daily_cap`, `muted`,
level `warn` when the daily cap is exhausted with candidates waiting. `ActionProvider` items:

- unfiled candidates → **File now** (bypasses `min_count_*` and `daily_cap` for one row) and
  **Mute**;
- filed rows → **Mute** (stops recurrence comments; the issue is left as is);
- muted rows → **Unmute**.

Mute writes `status = muted`, `muted_at`, `muted_by = <identity>` and is audited like every
other automation action. This is the entire Phase 1 UI; it reuses the automations page as built.

## 8. Config, safety, kill switches

- `CCSWITCH_ERROR_SINK_ENABLED` (default true) added to **both** `Settings` definitions in
  `server/config.py` and to `_MANAGED` in `server/api/settings.py`. False → `configure_logging()`
  does not install the handler; the automation then sees no new rows.
- Disabling the automation stops filing; capture continues, rows accumulate as `observed`
  (an inbox), same posture as session feedback.
- **Untrusted content.** Client messages are authenticated-tailnet-user-supplied text; server
  messages can embed user input (paths, prompts). Bodies are blockquoted under an explicit
  header, comment markers neutralized, redacted twice (record filter + `redact_text`), capped.
- **Bounded volume.** In-memory cap per flush window, count aggregation (a 10 000-record loop is
  one upsert), `daily_cap`, comment throttle, `ignore_loggers`, GitHub-native mute.
- **No new auth surface.** Filing uses the ambient `gh` path the CI doctor uses; the sink writes
  the DB the process already writes.
- **Failure isolation.** A DB outage makes `flush()` log once at DEBUG under the reentrancy
  guard and keep the pending map (bounded); a `gh` failure leaves the row retryable. Neither
  path can raise into the logging call site or the tick loop.

## 9. Testing

- `test_error_signature.py`: normalization table (uuids, hex, ints, quoted strings, paths);
  fingerprint stable across timestamps / ids / line numbers / app versions and different across
  level, logger, event, exception class; client fingerprint ignores line:col.
- `test_errsink.py`: aggregation (N records → one pending entry with count N); bound + drop
  counter; reentrancy (a record logged inside `flush()` is ignored); `client.error` counted once
  despite the bare re-emit; disabled flag installs nothing; flush upserts and refreshes the
  24 h-old sample; `stop()` drains.
- `test_issue_filing.py`: `decide` table over every branch in §6.1; `apply` with a fake `run`
  for file / comment / regress, and never-raise on a failing `run`.
- `test_error_triage.py`: `should_file` table (both thresholds, the warning window);
  first file two-phase with a failing then succeeding `gh`; recurrence comment and throttle;
  `not_planned` → muted; `completed` + recurrence → regression issue linking the old number;
  daily cap; `ignore_loggers`; retention never deletes `filed`/`muted`; File now / Mute /
  Unmute actions.
- `test_ipc_entrypoint.py` (existing, `slow`): the daemon entrypoint configures logging (#1423).
- `test_primary_lease.py` picks up `_error_triage_task` via `_PRIMARY_ONLY_LOOP_ATTRS`
  automatically; Postgres compat suite re-run for the new model.
- Web: no code change. The existing `web/src/obs` tests already cover capture.

## 10. Rollout and follow-ups

1. Land this spec; file the two migration issues (CI/CD Doctor `_reconcile` → `issue_filing`;
   session-feedback filer to be built on `issue_filing`, amending that spec's §6 sentence about
   generalizing `ci_issues.py`).
2. Implementation plan via `writing-plans`, executed subagent-driven. Suggested task order:
   daemon `configure_logging()` (#1423) → `error_signature` → model + migration → `errsink` +
   lifespan wiring → `ci_issues` additions → `issue_filing` → `error_triage` + orchestrator
   wiring → card actions → settings flag.
3. Enable `error-triage` in `file` mode on the primary once a day of `observed` rows looks sane
   (expect the first tick after enabling to file up to `daily_cap`).
4. Follow-ups, not in this spec: worker-daemon signature shipping; an Errors master/detail
   section; `console.error` interception in the browser (currently only uncaught errors,
   rejections and boundary catches are captured).

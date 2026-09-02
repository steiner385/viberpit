---
slug: observability
title: Daemon Observability
state: live
links:
  code:
    - server/obs/jsonlog.py
    - server/obs/loop_lag.py
    - server/obs/middleware.py
    - server/obs/context.py
    - server/obs/tasks.py
    - server/audit.py
    - server/app.py
  prs: [276, 763, 792, 793, 488, 320, 677, 144]
---

# Daemon Observability

**Problem** — ccswitch is a single-operator, self-hosted daemon that supervises long-lived `claude`
sessions and their WebSocket fan-out. When it misbehaves — the event loop stalls on blocking work, a
fire-and-forget task dies silently, a request 500s, a token leaks into a log — the operator has no
one to page and no external APM. Failures used to be invisible: a 130s loop stall logged nothing
about where (issue #762), a crashed background task only surfaced as a GC warning much later, and a
corrupt ledger 500'd `/api/accounts` while `/healthz` stayed green. The daemon has to make its own
health self-evident.

**Target users** — the operator diagnosing a wedged or slow daemon, and every other capability
(deploy health gate, fleet watchdog, dashboard) that reads the daemon's health surface to decide
what to do.

**Desired outcome** — every log line is structured JSON with correlation ids and secrets redacted;
event-loop stalls self-report with a stack dump of the blocking thread; fire-and-forget task crashes
are always observed and logged (with optional recovery); each request carries a correlation id
echoed to the browser; and a health endpoint surfaces daemon lifecycle, RPC-pool occupancy, and boot
outcomes.

**Success metrics** — no unretrieved-exception GC warnings from supervised tasks (a crash is logged
loudly with a traceback instead); a loop stall over threshold emits a warning plus the stuck
thread's stack; correlation ids appear on log records and as `X-Request-ID`; no credential-shaped
string reaches a log sink, structured field, or forwarded client log; `/api/daemon/health` answers
even while every RPC is failing.

**Non-goals** — a metrics time-series backend or dashboards (this is logs + point-in-time health,
not Prometheus/Grafana); distributed tracing across hosts (correlation is per-request within one
daemon); external alerting/paging (the watchdog and health endpoints expose state; routing alerts is
out of scope — see the fleet crash-loop gap #816).

**Notes** —
- **Structured JSON logging** (`server/obs/jsonlog.py`): `JSONFormatter` renders each record as a
  single JSON line with correlation fields from `ContextFilter`, structured `event`/`fields` from
  `log_event()`, and a full traceback on `exc_info`. Every line passes through the shared credential
  redactor (`server/audit.py`) so secrets never reach a sink — including structured fields and
  forwarded client logs the record-level filter doesn't cover. `configure_logging()` attaches a
  journald-captured stdout handler + a size-rotating file handler, is idempotent, and degrades to
  stdout-only rather than raising. Comprehensive error/lifecycle logging landed in PR #276.
- **Correlation context** (`server/obs/context.py`, `server/obs/middleware.py`):
  `RequestContextMiddleware` assigns a per-request id (honoring an inbound `X-Request-ID`), stamps it
  on every log record via a contextvar, echoes it as the `X-Request-ID` response header, and emits
  one `http.request` breadcrumb on completion; the contextvar token is always reset. `bind_session`
  ties records to a session id. PR #488 threaded `srcSeq` loss telemetry across
  daemon→edge→browser.
- **Loop-lag watchdog** (`server/obs/loop_lag.py`): sleeps a fixed interval and measures overshoot —
  time the loop spent blocked in synchronous work. Warnings are rate-limited (a saturated loop would
  emit ~86k lines/day and each warning is itself blocking work); suppressed stalls are counted and
  folded into the next warning, and `peak_lag_s` updates every tick. A `ThreadStallTracer` runs on a
  plain thread (schedulable while the loop is blocked) and snapshots the stuck thread's stack via
  `sys._current_frames()` — the pure-Python replacement (PR #793) for the segfaulting
  `faulthandler.dump_traceback_later` (issue #792), added to close issue #762 where the daemon
  blocked ~130s and logged nothing about where.
- **Task supervision** (`server/obs/tasks.py`): `supervise` attaches a done-callback that always
  retrieves a failed task's exception, logs it loudly with the task name, and invokes an optional
  recovery hook; clean returns and cancellations pass through untouched (audit W2.6 — a bare
  `create_task` crash left a frozen live tail with no log, restart, or client signal). Used across
  the app's background tasks (`server/app.py`).
- **Audit trail + redaction** (`server/audit.py`): `record_audit` writes an `AuditEvent` with
  credential material scrubbed from `identity` and `detail`; `RedactingFilter` strips `sk-ant-*`,
  `ccenc1:*`, padded base64 secrets, and signed-cookie shapes from log records; `redact_event`
  covers outbound WS event dicts (including batched lists). The base64 pattern carries a
  negative-lookbehind anchor to stay linear-time — PR #144 fixed a ReDoS that pinned the event loop
  for minutes on a ~200KB base64 blob. PR #677 suppressed noisy `CancelledError` pool-reset logs
  from sqlalchemy.pool.
- **Health surface** (`server/app.py`): `/healthz` proves the process is up and the DB schema
  matches (used unauthenticated by the deploy gate); `/api/daemon/health` surfaces daemon epoch /
  uptime / 24h restart count (issue #434), RPC-pool occupancy read from a sync cached field so it
  answers while every RPC is failing (issue #734), and boot outcomes (rehydrate/rearm counts + step
  errors, issue #820). A memory soft-guard + watchdog timer shipped in PR #320.

**Evidence** —
- `server/obs/jsonlog.py` — `JSONFormatter` + redacted structured logging, `configure_logging`.
- `server/obs/loop_lag.py` — `LoopLagWatchdog` + `ThreadStallTracer` stack-dump-on-stall.
- `server/obs/middleware.py` + `server/obs/context.py` — per-request correlation id + `log_event`.
- `server/obs/tasks.py` — `supervise` for fire-and-forget task crashes.
- `server/audit.py` — `record_audit`, `RedactingFilter`, `redact_event` redaction.
- `server/app.py` — `/healthz`, `/api/daemon/health`, loop-lag wiring.
- PRs: #276 (error/lifecycle logging), #763 (stall stack dump), #792 (faulthandler segfault),
  #793 (GIL-safe thread tracer), #488 (srcSeq loss telemetry), #320 (watchdog + memory soft-guard),
  #677 (suppress pool-reset noise), #144 (redaction ReDoS fix).

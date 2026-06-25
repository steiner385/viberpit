# Comprehensive Error & Lifecycle Logging — Design

**Date:** 2026-06-24
**Status:** Approved (design); pending implementation plan
**Author:** brainstormed with Claude

## Problem

`ccswitch` exhibits strange, hard-to-diagnose session behavior — especially around
**upgrades** (each merge to `master` redeploys, restarts the daemon, kills
`run_in_background` tasks, re-attaches sessions, and re-scans background tasks). Today there is
**almost no durable record** to diagnose from:

- Backend logs stream to stdout/stderr, but the systemd units do **not** capture them to journald,
  so logs are lost on restart — i.e. lost at exactly the moment of interest.
- No structured logging, no correlation IDs: a single user action crossing
  client → WS → orchestrator → driver → subprocess produces scattered, unjoinable text lines.
- No catch-all FastAPI exception handler: unhandled exceptions return a default 500 with nothing
  logged specially.
- Frontend has **no** error boundary, **no** `window.onerror`/`unhandledrejection` handlers, and
  **no** way to forward browser errors to the server. React render crashes white-screen silently.
- Many restart-time bugs produce **no error at all** — they are state-drift bugs (a session stuck
  "waiting" after a restart). Pure error logging cannot catch these; they need a lifecycle
  breadcrumb trail.

## Goal

Log **all** errors and warnings across frontend and backend with maximum useful detail, plus
**lifecycle breadcrumbs** around deploy/restart and per-session state transitions, all
**correlated** by `session_id` and `request_id`, persisted durably to **journald + rotating JSON
files**, so that "weird behavior during an upgrade" becomes identifiable, diagnosable, and
resolvable.

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Log sink | **journald + rotating JSON files** on the host |
| Frontend capture | **Full capture + forward** (ErrorBoundary, global handlers, WS logging, batched POST to backend) |
| Lifecycle trace | **Yes — deploy + session lifecycle breadcrumbs** (INFO), beyond just errors/warnings |
| Correlation | **`session_id` + `request_id`** threaded through all logs |
| Implementation | **Approach A** — stdlib `logging` + JSON formatter + `contextvars` (no new backend deps; reuses existing redaction filter; every existing call site becomes structured with no edits) |

### Why Approach A (stdlib logging) over structlog

Riding the existing Python `logging` tree means all 40+ files already calling
`log.info/warning/exception(...)` become structured **and** correlated with **zero call-site
changes**. It reuses the existing `RedactingFilter` (so secrets stay redacted), respects the
project's single-source-of-truth norm (no split between "old plain" and "new structured" logging),
and lands JSON in both journald and rotating files. `structlog` was rejected because it adds a
dependency and would either fork the codebase into two logging styles or require a large migration.

## Architecture

A new `server/obs/` (observability) package owns the logging substrate, kept separate from
`app.py`/`audit.py` so it has one clear responsibility. The frontend gets a `web/src/obs/` logger
plus capture wiring. A single ingest endpoint replays client logs back through the **same** Python
logging pipeline, so frontend and backend logs share one redaction filter, one rotation policy, and
one query surface — joinable by `session_id`/`request_id`.

```
browser error ─▶ web/src/obs/logger (ring buffer, batched)
                      │  POST /api/client-logs  (auth, session_id, X-Request-ID)
                      ▼
              server/api/client_logs ──▶ Python logging pipeline ──┐
                                                                    │
backend log.* / log_event(...) ─────────────────────────────────▶ ┤─▶ RedactingFilter
                                                                    │
                              ContextFilter (session_id/request_id/proc_epoch)
                                                                    │
                                          ┌─────────────────────────┴───────────────┐
                                          ▼                                          ▼
                               StreamHandler(stdout)                     RotatingFileHandler
                                   → journald                         → CCSWITCH_LOG_DIR/ccswitch.jsonl
```

---

## Section 1 — Backend structured-logging substrate

New package `server/obs/`:

### `server/obs/jsonlog.py`
- **`JSONFormatter`** — emits one JSON object per line with fields:
  - `ts` (via the existing `server/time_iso.py:iso_utc`, **never** a bare `.isoformat()` — see the
    naive-UTC bug history),
  - `level`, `logger`, `msg`, `module`, `func`, `line`,
  - correlation fields (`session_id`, `request_id`, `proc_epoch`),
  - any structured `extra` fields (from `log_event`),
  - full exception traceback when `exc_info` is present.
  - Non-serializable `extra` values are coerced to `repr` so the formatter never raises.
- **`ContextFilter`** — reads the `contextvars` and stamps `session_id` / `request_id` /
  `proc_epoch` onto every record.
- **`configure_logging()`** — idempotent. Sets root level from `CCSWITCH_LOG_LEVEL` (default
  `INFO`); attaches two handlers — a stdout `StreamHandler` (→ journald) and a
  `RotatingFileHandler` writing `ccswitch.jsonl` under `CCSWITCH_LOG_DIR`; then calls the existing
  `install_redacting_filter()` so redaction covers both handlers. If `CCSWITCH_LOG_DIR` is
  unwritable, it falls back to **stdout-only** and emits a single warning — it never raises.

### `server/obs/context.py`
- `contextvars`: `session_id_var`, `request_id_var`, `proc_epoch` (a value fixed at process
  start — start-time + pid — stamped on **every** record so before/after-restart lines are
  trivially distinguishable).
- Helpers: `bind_session(sid)` and `bind_request(rid)` context managers (set + reset token), and
  `log_event(logger, level, event, **fields)` which emits with `extra={"event": event, **fields}`
  for rich new-code events.

### Wiring
- `configure_logging()` replaces the bare redaction-install call at `server/app.py:103`, run first
  in the FastAPI lifespan so every later log line is structured.

### Principle
The logging system must never take the app down: file errors fall back to stdout, logging failures
are swallowed, buffers are bounded.

---

## Section 2 — Correlation plumbing & catch-all handler

### Request ID (HTTP)
- `server/obs/middleware.py:RequestContextMiddleware` (ASGI). Per request: generate a 12-char
  uuid4-hex `request_id`, set the contextvar, add an `X-Request-ID` response header, and emit one
  INFO `http.request` breadcrumb on completion with `method`, `path`, `status`, `duration_ms`. The
  contextvar token is always reset in `finally`, so IDs never leak between requests.

### Session ID
- Set at the two points a session enters scope: the WS bridge (`server/bridge/ws.py`) on attach,
  and inside each per-session orchestrator coroutine.
- **Critical:** `contextvars` do **not** auto-propagate into tasks spawned without `copy_context` —
  exactly the orchestrator's `run_in_background` pattern. Each session-scoped background coroutine
  therefore calls `bind_session(sid)` at its top rather than assuming inheritance.

### Catch-all exception handler
- Add `@app.exception_handler(Exception)` alongside the existing `DaemonUnavailable`/`DaemonError`
  handlers. It logs `log.exception(...)` (full traceback, auto-stamped with `request_id`) then
  returns a generic 500 JSON. FastAPI resolves specific handlers first, so existing 503/409 domain
  mappings still win; only genuinely-unhandled errors fall through.

---

## Section 3 — Lifecycle breadcrumbs (deploy + session)

INFO-level `log_event(...)` calls that make "weird state after upgrade" reconstructable even when
nothing threw. Confined to genuine state transitions and the deploy sequence — per-poll / per-frame
chatter stays at DEBUG (off by default).

### Deploy / restart sequence
Instrument the existing graceful-drain handshake (`server/deploy/app.py` + the orchestrator drain
path) with a shared `deploy_id`:
- `deploy.drain.start` (in-flight session count) → `deploy.daemon.stop` → `deploy.daemon.start`
  (new `proc_epoch`) → `deploy.sessions.reattach` (recovered vs lost counts) →
  `deploy.edge.cutover`.

Filtering `proc_epoch=old` vs `proc_epoch=new` around a `deploy_id` shows exactly what each session
did across the restart.

### Per-session transitions
At each orchestrator state change, emit `session.<transition>` with `session_id`, `from`, `to`,
`reason`:
- `start`, `resume`, `reattach` (after restart), `rotate` (account switch),
  `bg_task_rescan` (the `scan_open_background_tasks` pass — logs killed/open/rearm counts, the exact
  signal behind the "killed bg-task pins waiting_external" class of bug), `park`/`unpark`, and any
  transition into a terminal/`killed`/`error` status.
- `from`/`to` values **reuse the existing derived-status vocabulary** (the same status strings
  already persisted to the DB) — not a new parallel enum. Logging both ends makes an *illegal*
  transition (its own bug signature) visible.

---

## Section 4 — Frontend capture + forwarding, and ingest endpoint

### Client logger — `web/src/obs/logger.ts`
- Singleton with a **bounded ring buffer** (~200 entries, drop-oldest) and **batched delivery**:
  flush on a ~5s timer, on a size threshold, and on `visibilitychange`/`pagehide` (last-gasp flush
  via `navigator.sendBeacon` so tab-close errors aren't lost — critical during a deploy restart
  when WS drops and users reload).
- Each entry carries: `level`, `msg`, `stack`, `url`, `userAgent`, `client_id` (per-tab uuid),
  `app_version` (Vite build hash), the `X-Request-ID` of the originating API call when available,
  and the **currently-focused `session_id`** so browser errors share the backend's session spine.

### Capture points
- `window.addEventListener('error', …)` and `'unhandledrejection'` (neither exists today).
- A React **`ErrorBoundary`** wrapping the app: `componentDidCatch` → logger + a non-blank fallback
  UI (replaces today's silent white-screen on render crash).
- **WS client** (`web/src/ws/client.ts`): log `onclose` with `code`/`reason`, distinguishing
  expected (1000 / navigation) from unexpected (1006 / 1011) disconnects; log `onerror`.
- Reuse the existing `MutationCache.onError` path so forwarded errors and the user-facing toast come
  from one place.

### Ingest endpoint — `POST /api/client-logs` (`server/api/client_logs.py`)
- Accepts a JSON batch; **auth via the existing tailnet-identity dependency** (not anonymous). Caps
  batch size and per-message length; rate-limited per identity so a buggy client can't flood the
  sink.
- Each entry is replayed **into the same Python logging pipeline** at the mapped level with
  `source="frontend"`, the payload's `session_id` bound into the contextvar — so **redaction applies
  to client logs too**, and they share the journald + `ccswitch.jsonl` sink. No separate frontend
  log store.

---

## Section 5 — Infra, config & retention

### Systemd
Add to both `deploy/ccswitch.service` and `deploy/ccswitch-daemon.service`:
- `StandardOutput=journal`, `StandardError=journal`, `SyslogIdentifier=ccswitch` (daemon unit gets
  its own identifier so the two are distinguishable).

This alone is the instant restart-survival win: `journalctl -u ccswitch` retains JSON across
restarts.

### Config (`server/config.py` — the existing source of truth)
- `CCSWITCH_LOG_DIR` — default `/var/log/ccswitch` in prod; falls back to a writable per-user dir in
  dev.
- `CCSWITCH_LOG_LEVEL` — default `INFO`.
- `CCSWITCH_CLIENT_LOG_LEVEL` — floor for what the frontend forwards (default `warn`).

### Rotation / retention
- `RotatingFileHandler` at `maxBytes=50MB × backupCount=10` (~550MB cap, drop-oldest). No
  time-based purge — size-bounded is self-limiting.
- Document the new vars in the deploy `.env.example` / docs.

---

## Section 6 — Error handling & testing

### Error handling of the logging system itself (must never break the app)
- Unwritable log dir → stdout-only fallback + one warning, no raise.
- `/api/client-logs` failures → frontend silently drops the batch; it never logs-about-failing-to-log
  (no recursion). Bounded buffers on both sides.
- Formatter coerces non-serializable `extra` values to `repr`.

### Testing (TDD; both CI gates)
**Backend (`pytest`):**
- `JSONFormatter` field/exception/serialization coverage.
- Redaction still strips secrets from JSON output.
- `ContextFilter` injects + clears correlation fields.
- `RequestContextMiddleware` sets contextvar, emits `X-Request-ID`, resets on error.
- Catch-all handler logs with traceback + returns 500.
- `/api/client-logs` ingest — auth 401 path, batch/level mapping, size caps, redaction.

**Frontend (`vitest` + `tsc -b` + `eslint`):**
- Logger batching / flush / ring-buffer drop-oldest.
- `sendBeacon` on `pagehide`.
- `ErrorBoundary` catches & reports & renders fallback.
- Global `error`/`unhandledrejection` handlers wired.
- WS `onclose` code logging.

**Manual verification:**
- Confirm `journalctl -u ccswitch` shows JSON and `ccswitch.jsonl` is written. (Worktree caveats
  apply — run backend against the prebuilt env with `PYTHONPATH=<wt>/src`; for web,
  `rm -f web/node_modules && npm ci`.)

## Out of scope (YAGNI)

- External aggregator (Sentry/Loki) — explicitly deferred; revisit if/when public-hosting (P4).
- In-app/DB log viewer UI — not chosen; journald + JSON files + correlation cover the diagnosis
  workflow.
- Time-based log retention/archival — size-bounded rotation is sufficient.
- Distributed tracing spans — `session_id`/`request_id` correlation is enough at current scale.

## Constitution / norms compliance

- **VI (single source of truth):** rides the existing `logging` tree + `RedactingFilter`; reuses
  derived-status vocabulary; one ingest path for client logs (no parallel store).
- **IV (security-first):** redaction applies to all sinks including client logs; ingest endpoint is
  authed + rate-limited; no data leaves the box.
- **VII (modern vetted deps):** no new backend dependency (stdlib only).
- **V (TDD + quality gates):** tests specified for every new unit on both gates.
